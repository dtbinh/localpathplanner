from enum import Enum
from typing import Tuple, List
from math import degrees, asin, atan2

import numpy as np
import vrep
import cv2


def log_and_retry(func):
    """
    A decorator that tries to execute the function until no
    connection error is raised.
    """

    def func_wrapper(self, *args):
        while True:
            try:
                return func(self, *args)
            except ConnectionError as e:
                print("Error in {}: {}".format(func, e))
                continue
    return func_wrapper


class VRepError(Enum):
    NO_VALUE = 1
    TIMEOUT = 2
    ILLEGAL_OPMODE = 4
    SERVER_ERROR = 8
    SPLIT_PROGRESS = 16
    LOCAL_ERROR = 32
    INIT_ERROR = 64

    @staticmethod
    def create(return_value: int) -> Tuple:
        """
        Returns all the errors associated with a return value.
        """
        if return_value == 0:
            return tuple()
        else:
            return tuple(
                err for err in VRepError
                if bool(return_value & err.value))


class VRepObject:
    """
    Simple wrapper around the V-Rep Remote API
    """
    BLOCK = vrep.simx_opmode_blocking

    def __init__(self, client_id: int, handle: int, name: str):
        self.client_id = client_id
        self.name = name
        self.handle = handle

    @log_and_retry
    def duplicate(self):
        ret, handles = vrep.simxCopyPasteObjects(self.client_id, [self.handle], self.BLOCK)
        if ret == 0:
            # TODO return proper objects, we are not savages.
            return handles
        else:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def get_position(self, other: "VRepObject" = None) -> np.ndarray:
        """Retrieve the object position.
        :type other: VRepObject

        :param other: if specified, result will be relative to `other`.
        If None, retrieve the absolute position.

        """
        handle = -1
        if other:
            handle = other.handle

        ret, pos = vrep.simxGetObjectPosition(self.client_id, self.handle, handle, self.BLOCK)
        if ret == 0:
            return np.array(pos, np.float32)
        else:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def get_velocity(self) -> Tuple:
        ret, linear, angular = vrep.simxGetObjectVelocity(self.client_id, self.handle, self.BLOCK)
        if ret == 0:
            return np.array(linear, np.float32), np.array(angular, np.float32)
        else:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def get_bbox(self) -> Tuple[np.ndarray, np.ndarray]:

        coords = tuple(
            vrep.simxGetObjectFloatParameter(self.client_id,
                                             self.handle, i, self.BLOCK)[1]
            for i in range(15, 21))
        return np.array(coords[:3], np.float32), \
               np.array(coords[3:], np.float32)

    @log_and_retry
    def get_spherical(self, other: "VRepObject" = None, offset: object = (0, 0, 0)) -> object:
        """Spherical coordinates of object.

        Azimuth is CCW from X axis.
        0       Front
        90      Leftside
        +/-180  Back
        -90     Rightside

        Elevation is respective to the XY plane.
        0       Horizon
        90      Zenith
        -90     Nadir
        """
        while True:
            try:
                pos = self.get_position(other)
                pos += offset
                dist = np.linalg.norm(pos)
                azimuth = degrees(atan2(pos[1], pos[0]))
                elevation = degrees(asin(pos[2] / dist))
                return dist, azimuth, elevation
            except ConnectionError:
                continue

    @log_and_retry
    def get_orientation(self, other: "VRepObject" = None):
        """Retrieve the object orientation (as Euler angles)
        """
        handle = -1
        if other:
            handle = other.handle

        ret, euler = vrep.simxGetObjectOrientation(
            self.client_id, self.handle, handle, self.BLOCK)
        if ret == 0:
            return np.array(euler, np.float32)
        else:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def set_position(self, pos, other: "VRepObject" = None):
        """Sets the position.

        pos: 3-valued list or np.array (x,y,z coordinates in meters)
        """
        handle = -1
        if other:
            handle = other.handle

        ret = vrep.simxSetObjectPosition(self.client_id, self.handle, handle, pos, self.BLOCK)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def set_orientation(self, euler: Tuple[float, float, float]):
        """
        Sets the absolute orientation of the object
        """
        ret = vrep.simxSetObjectOrientation(self.client_id, self.handle, -1, euler, self.BLOCK)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))


class VRepDepthSensor(VRepObject):

    def __init__(self, client_id: int, handle: int, name: str):
        """Initialize sensor and get specific information

        Initialize
        """
        super().__init__(client_id, handle, name)
        # Assume that the sensor returns a square image
        __, self.res = vrep.simxGetObjectIntParameter(
            client_id,
            handle,
            vrep.sim_visionintparam_resolution_x,
            self.BLOCK)
        __, self.max_depth = vrep.simxGetObjectFloatParameter(
            client_id,
            handle,
            vrep.sim_visionfloatparam_far_clipping,
            self.BLOCK)
        __, angle_radians = vrep.simxGetObjectFloatParameter(
            client_id,
            handle,
            vrep.sim_visionfloatparam_perspective_angle,
            self.BLOCK)
        self.angle = round(degrees(angle_radians), 3)

    @log_and_retry
    def get_depth_buffer(self) -> np.ndarray:
        ret, res, d = vrep.simxGetVisionSensorDepthBuffer(self.client_id, self.handle, self.BLOCK)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))
        else:
            d = np.array(d, np.float32).reshape((res[1], res[0]))
            d = np.flipud(d)  # the depth buffer is upside-down
            d = cv2.resize(d, (256, 256))  # TODO make codebase resolution-agnostic
            return res, d

    def get_dilated_depth_buffer(self, radius_f) -> np.ndarray:
        """Dilates a float image according to pixel depth.

        The input image is sliced by pixel intensity: (1, 0.9], (0.9, 0.8] etc.
        Each slice is dilated by a kernel which grows in size as the values
        get smaller. The slices are then fused back together, lower slice
        overwrite higher ones.

        :param radius_f: a function that converts meters to pixels
        :return: The dilated depth map
        """
        __, im = self.get_depth_buffer()
        acc = np.ones_like(im)

        for i in np.arange(1, 0.1, -0.1):
            im_slice = im.copy()
            im_slice[(im_slice <= i - 0.1) | (im_slice > i)] = 0

            ker_size = 2 * radius_f((i - 0.1) * self.max_depth)

            ker = np.ones((ker_size, ker_size), np.uint8)
            im_slice = cv2.dilate(im_slice, ker)

            # Replace "older" values
            acc = np.where(im_slice != 0, im_slice, acc)
        return acc


class VRepDummy(VRepObject):
    pass


class VRepClient:
    def __init__(self, host: str, port: int):
        self.id = vrep.simxStart(host, port, True, True, -100, 5)
        if self.id == -1:
            raise ConnectionError("Connection to {}:{} failed".format(host, port))

    def get_object(self, name: str) -> VRepObject or VRepDepthSensor:
        ret, handle = vrep.simxGetObjectHandle(self.id, name, vrep.simx_opmode_blocking)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))
        else:
            """
            Kludge ahead.
            To find out whether the object is a depth sensor, we query its x-resolution (parameter 1002).
            If we don't get a server error, we know it is a sensor.
            """
            ret, __ = vrep.simxGetObjectIntParameter(self.id, handle, 1002, vrep.simx_opmode_blocking)
            if ret == 0:
                return VRepDepthSensor(self.id, handle, name)
            else:
                return VRepObject(self.id, handle, name)

    # TODO wrap return codes in exceptions
    def create_dummy(self, pos: List[float], size: float = 0.2):
        ret, dummy_handle = vrep.simxCreateDummy(self.id, size, None, vrep.simx_opmode_blocking)
        vrep.simxSetObjectPosition(self.id, dummy_handle, -1, pos, vrep.simx_opmode_blocking)
        # return VRepDummy(self._conn_id, dummy_handle)

    def close_connection(self):
        vrep.simxFinish(self.id)
