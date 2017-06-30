from __future__ import division, print_function

from enum import Enum
from math import degrees, asin, atan2

import numpy as np
import vrep
import cv2
import sys

if sys.version_info < (3,0):
    class ConnectionError(OSError):
        pass

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
                print("Error in ", func, ": ", e, sep="")
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
    def create(return_value):
        """
        Returns all the errors associated with a return value.
        """
        if return_value == 0:
            return tuple()
        else:
            return tuple(
                err for err in VRepError
                if bool(return_value & err.value))


class VRepObject(object):
    """
    Simple wrapper around the V-Rep Remote API
    """
    BLOCK = vrep.simx_opmode_blocking

    def __init__(self, client_id, handle, name):
        # type: (int, int, str)
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
    def get_position(self, other= None):
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
    def get_velocity(self):
        ret, linear, angular = vrep.simxGetObjectVelocity(self.client_id, self.handle, self.BLOCK)
        if ret == 0:
            return np.array(linear, np.float32), np.array(angular, np.float32)
        else:
            raise ConnectionError(VRepError.create(ret))

    @log_and_retry
    def get_spherical(self, other= None, offset=(0, 0, 0)):
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
    def get_orientation(self, other=None):
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
    def set_position(self, pos, other=None):
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
    def set_orientation(self, euler):
        """
        Sets the absolute orientation of the object
        """
        ret = vrep.simxSetObjectOrientation(self.client_id, self.handle, -1, euler, self.BLOCK)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))


class VRepDepthSensor(VRepObject):
    @log_and_retry
    def get_depth_buffer(self):
        ret, res, d = vrep.simxGetVisionSensorDepthBuffer(self.client_id, self.handle, self.BLOCK)
        if ret != 0:
            raise ConnectionError(VRepError.create(ret))
        else:
            d = np.array(d, np.float32).reshape((res[1], res[0]))
            d = np.flipud(d)  # the depth buffer is upside-down
            d = cv2.resize(d, (256, 256))  # TODO make codebase resolution-agnostic
            return res, d


class VRepDummy(VRepObject):
    pass


class VRepClient(object):
    def __init__(self, host, port):
        # type: (str, int)
        self.id = vrep.simxStart(host, port, True, True, -100, 5)
        if self.id == -1:
            raise ConnectionError("Connection to {}:{} failed".format(host, port))

    def get_object(self, name):
        # type: (str)
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
    def create_dummy(self, pos, size = 0.2):
        ret, dummy_handle = vrep.simxCreateDummy(self.id, size, None, vrep.simx_opmode_blocking)
        vrep.simxSetObjectPosition(self.id, dummy_handle, -1, pos, vrep.simx_opmode_blocking)
        # return VRepDummy(self._conn_id, dummy_handle)

    def close_connection(self):
        vrep.simxFinish(self.id)
