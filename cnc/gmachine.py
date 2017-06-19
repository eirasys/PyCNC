from __future__ import division

import cnc.logging_config as logging_config
from cnc import hal
from cnc.pulses import *
from cnc.coordinates import *
from cnc.heater import *
from cnc.enums import *


class GMachineException(Exception):
    """ Exceptions while processing gcode line.
    """
    pass


class GMachine(object):
    """ Main object which control and keep state of whole machine: steppers,
        spindle, extruder etc
    """
    def __init__(self):
        """ Initialization.
        """
        self._position = Coordinates(0.0, 0.0, 0.0, 0.0)
        # init variables
        self._velocity = 0
        self._spindle_rpm = 0
        self._pause = 0
        self._local = None
        self._convertCoordinates = 0
        self._absoluteCoordinates = 0
        self._plane = None
        self._fan_state = False
        self._heaters = dict()
        self.reset()
        hal.init()

    def release(self):
        """ Return machine to original position and free all resources.
        """
        self.home()
        self._spindle(0)
        for h in self._heaters:
            self._heaters[h].stop()
        hal.deinit()

    def reset(self):
        """ Reinitialize all program configurable thing.
        """
        self._velocity = 1000
        self._spindle_rpm = 1000
        self._pause = 0
        self._local = Coordinates(0.0, 0.0, 0.0, 0.0)
        self._convertCoordinates = 1.0
        self._absoluteCoordinates = True
        self._plane = PLANE_XY

    # noinspection PyMethodMayBeStatic
    def _spindle(self, spindle_speed):
        hal.join()
        hal.spindle_control(100.0 * spindle_speed / SPINDLE_MAX_RPM)

    def _fan(self, state):
        hal.fan_control(state)
        self._fan_state = state

    def _heat(self, heater, temperature, wait):
        # check if sensor is ok
        if heater == HEATER_EXTRUDER:
            measure = hal.get_extruder_temperature
            control = hal.extruder_heater_control
            coefficients = EXTRUDER_PID
        elif heater == HEATER_BED:
            measure = hal.get_bed_temperature
            control = hal.bed_heater_control
            coefficients = BED_PID
        else:
            raise GMachineException("unknown heater")
        try:
            measure()
        except (IOError, OSError):
            raise GMachineException("can not measure temperature")
        if heater in self._heaters:
            self._heaters[heater].stop()
            del self._heaters[heater]
        if temperature != 0:
            self._heaters[heater] = Heater(temperature, coefficients, measure,
                                           control)
            if wait:
                self._heaters[heater].wait()

    def __check_delta(self, delta):
        pos = self._position + delta
        if not pos.is_in_aabb(Coordinates(0.0, 0.0, 0.0, 0.0),
                              Coordinates(TABLE_SIZE_X_MM, TABLE_SIZE_Y_MM,
                                          TABLE_SIZE_Z_MM, 0)):
            raise GMachineException("out of effective area")

    def _move_linear(self, delta, velocity):
        delta = delta.round(1.0 / STEPPER_PULSES_PER_MM_X,
                            1.0 / STEPPER_PULSES_PER_MM_Y,
                            1.0 / STEPPER_PULSES_PER_MM_Z,
                            1.0 / STEPPER_PULSES_PER_MM_E)
        if delta.is_zero():
            return
        self.__check_delta(delta)

        logging.info("Moving linearly {}".format(delta))
        gen = PulseGeneratorLinear(delta, velocity)
        hal.move(gen)
        # save position
        self._position = self._position + delta

    @staticmethod
    def __quarter(pa, pb):
        if pa >= 0 and pb >= 0:
            return 1
        if pa < 0 and pb >= 0:
            return 2
        if pa < 0 and pb < 0:
            return 3
        if pa >= 0 and pb < 0:
            return 4

    def __adjust_circle(self, da, db, ra, rb, direction, pa, pb, ma, mb):
        r = math.sqrt(ra * ra + rb * rb)
        if r == 0:
            raise GMachineException("circle radius is zero")
        sq = self.__quarter(-ra, -rb)
        if da == 0 and db == 0:  # full circle
            ea = da
            eb = db
            eq = 5  # mark as non-existing to check all
        else:
            b = (db - rb) / (da - ra)
            ea = math.copysign(math.sqrt(r * r / (1.0 + abs(b))), da - ra)
            eb = math.copysign(math.sqrt(r * r - ea * ea), db - rb)
            eq = self.__quarter(ea, eb)
            ea += ra
            eb += rb
        # iterate coordinates quarters and check if we fit table
        q = sq
        pq = q
        for _ in range(0, 4):
            if direction == CW:
                q -= 1
            else:
                q += 1
            if q <= 0:
                q = 4
            elif q >= 5:
                q = 1
            if q == eq:
                break
            is_raise = False
            if (pq == 1 and q == 4) or (pq == 4 and q == 1):
                is_raise = (pa + ra + r > ma)
            elif (pq == 1 and q == 2) or (pq == 2 and q == 1):
                is_raise = (pb + rb + r > mb)
            elif (pq == 2 and q == 3) or (pq == 3 and q == 2):
                is_raise = (pa + ra - r < 0)
            elif (pq == 3 and q == 4) or (pq == 4 and q == 3):
                is_raise = (pb + rb - r < 0)
            if is_raise:
                raise GMachineException("out of effective area")
            pq = q
        return ea, eb

    def _circular(self, delta, radius, velocity, direction):
        delta = delta.round(1.0 / STEPPER_PULSES_PER_MM_X,
                            1.0 / STEPPER_PULSES_PER_MM_Y,
                            1.0 / STEPPER_PULSES_PER_MM_Z,
                            1.0 / STEPPER_PULSES_PER_MM_E)
        radius = radius.round(1.0 / STEPPER_PULSES_PER_MM_X,
                              1.0 / STEPPER_PULSES_PER_MM_Y,
                              1.0 / STEPPER_PULSES_PER_MM_Z,
                              1.0 / STEPPER_PULSES_PER_MM_E)
        self.__check_delta(delta)
        # get delta vector and put it on circle
        circle_end = Coordinates(0, 0, 0, 0)
        if self._plane == PLANE_XY:
            circle_end.x, circle_end.y = \
                self.__adjust_circle(delta.x, delta.y, radius.x, radius.y,
                                     direction, self._position.x,
                                     self._position.y, TABLE_SIZE_X_MM,
                                     TABLE_SIZE_Y_MM)
            circle_end.z = delta.z
        elif self._plane == PLANE_YZ:
            circle_end.y, circle_end.z = \
                self.__adjust_circle(delta.y, delta.z, radius.y, radius.z,
                                     direction, self._position.y,
                                     self._position.z, TABLE_SIZE_Y_MM,
                                     TABLE_SIZE_Z_MM)
            circle_end.x = delta.x
        elif self._plane == PLANE_ZX:
            circle_end.z, circle_end.x = \
                self.__adjust_circle(delta.z, delta.x, radius.z, radius.x,
                                     direction, self._position.z,
                                     self._position.x, TABLE_SIZE_Z_MM,
                                     TABLE_SIZE_X_MM)
            circle_end.y = delta.y
        circle_end.e = delta.e
        circle_end = circle_end.round(1.0 / STEPPER_PULSES_PER_MM_X,
                                      1.0 / STEPPER_PULSES_PER_MM_Y,
                                      1.0 / STEPPER_PULSES_PER_MM_Z,
                                      1.0 / STEPPER_PULSES_PER_MM_E)
        logging.info("Moving circularly {} {} {} with radius {}"
                     " and velocity {}".format(self._plane, circle_end,
                                               direction, radius, velocity))
        gen = PulseGeneratorCircular(circle_end, radius, self._plane, direction,
                                     velocity)
        hal.move(gen)
        # if finish coords is not on circle, move some distance linearly
        linear_delta = delta - circle_end
        if not linear_delta.is_zero():
            logging.info("Moving additionally {} to finish circle command".
                         format(linear_delta))
            gen = PulseGeneratorLinear(linear_delta, velocity)
            hal.move(gen)
        # save position
        self._position = self._position + circle_end + linear_delta

    def home(self):
        """ Move head to park position
        """
        d = Coordinates(0, 0, -self._position.z, 0)
        self._move_linear(d, STEPPER_MAX_VELOCITY_MM_PER_MIN)
        d = Coordinates(-self._position.x, -self._position.y, 0, 0)
        self._move_linear(d, STEPPER_MAX_VELOCITY_MM_PER_MIN)

    def position(self):
        """ Return current machine position (after the latest command)
            Note that hal might still be moving motors and in this case
            function will block until motors stops.
            This function for tests only.
            :return current position.
        """
        hal.join()
        return self._position

    def plane(self):
        """ Return current plane for circular interpolation. This function for
            tests only.
            :return current plane.
        """
        return self._plane

    def fan_state(self):
        """ Check if fan is on.
            :return True if fan is on, False otherwise.
        """
        return self._fan_state

    def __get_target_temperature(self, heater):
        if heater not in self._heaters:
            return 0
        return self._heaters[heater].target_temperature()

    def extruder_target_temperature(self):
        """ Return desired extruder temperature.
            :return Temperature in Celsius, 0 if disabled.
        """
        return self.__get_target_temperature(HEATER_EXTRUDER)

    def bed_target_temperature(self):
        """ Return desired bed temperature.
            :return Temperature in Celsius, 0 if disabled.
        """
        return self.__get_target_temperature(HEATER_BED)

    def do_command(self, gcode):
        """ Perform action.
        :param gcode: GCode object which represent one gcode line
        :return String if any answer require, None otherwise.
        """
        if gcode is None:
            return None
        answer = None
        logging.debug("got command " + str(gcode.params))
        # read command
        c = gcode.command()
        if c is None and gcode.has_coordinates():
            c = 'G1'
        # read parameters
        if self._absoluteCoordinates:
            coord = gcode.coordinates(self._position, self._convertCoordinates)
            coord = coord + self._local
            delta = coord - self._position
        else:
            delta = gcode.coordinates(Coordinates(0.0, 0.0, 0.0, 0.0),
                                      self._convertCoordinates)
            # coord = self._position + delta
        velocity = gcode.get('F', self._velocity)
        pause = gcode.get('P', self._pause)
        radius = gcode.radius(Coordinates(0.0, 0.0, 0.0, 0.0),
                              self._convertCoordinates)
        # check parameters
        if velocity <= 0 or velocity > STEPPER_MAX_VELOCITY_MM_PER_MIN:
            raise GMachineException("bad feed speed")
        if pause < 0:
            raise GMachineException("bad delay")
        # select command and run it
        if c == 'G0':  # rapid move
            self._move_linear(delta, STEPPER_MAX_VELOCITY_MM_PER_MIN)
        elif c == 'G1':  # linear interpolation
            self._move_linear(delta, velocity)
        elif c == 'G2':  # circular interpolation, clockwise
            self._circular(delta, radius, velocity, CW)
        elif c == 'G3':  # circular interpolation, counterclockwise
            self._circular(delta, radius, velocity, CCW)
        elif c == 'G4':  # delay in s
            hal.join()
            time.sleep(pause)
        elif c == 'G17':  # XY plane select
            self._plane = PLANE_XY
        elif c == 'G18':  # ZX plane select
            self._plane = PLANE_ZX
        elif c == 'G19':  # YZ plane select
            self._plane = PLANE_YZ
        elif c == 'G20':  # switch to inches
            self._convertCoordinates = 25.4
        elif c == 'G21':  # switch to mm
            self._convertCoordinates = 1.0
        elif c == 'G28':  # home
            self.home()
        elif c == 'G53':  # switch to machine coords
            self._local = Coordinates(0.0, 0.0, 0.0, 0.0)
        elif c == 'G90':  # switch to absolute coords
            self._absoluteCoordinates = True
        elif c == 'G91':  # switch to relative coords
            self._absoluteCoordinates = False
        elif c == 'G92':  # switch to local coords
            self._local = self._position - \
                          gcode.coordinates(Coordinates(0.0, 0.0, 0.0, 0.0),
                                            self._convertCoordinates)
        elif c == 'M3':  # spindle on
            spindle_rpm = gcode.get('S', self._spindle_rpm)
            if spindle_rpm < 0 or spindle_rpm > SPINDLE_MAX_RPM:
                raise GMachineException("bad spindle speed")
            self._spindle(spindle_rpm)
            self._spindle_rpm = spindle_rpm
        elif c == 'M5':  # spindle off
            self._spindle(0)
        elif c == 'M2' or c == 'M30':  # program finish, reset everything.
            self.reset()
        # extruder and bed heaters control
        elif c == 'M104' or c == 'M109' or c == 'M140' or c == 'M190':
            if c == 'M104' or c == 'M109':
                heater = HEATER_EXTRUDER
            elif c == 'M140' or c == 'M190':
                heater = HEATER_BED
            else:
                raise Exception("Unexpected heater command")
            wait = c == 'M109' or c == 'M190'
            if not gcode.has("S"):
                raise GMachineException("temperature is not specified")
            t = gcode.get('S', 0)
            if ((heater == HEATER_EXTRUDER and t > EXTRUDER_MAX_TEMPERATURE) or
                    (heater == HEATER_BED and t > BED_MAX_TEMPERATURE) or
                    t < MIN_TEMPERATURE) and t != 0:
                raise GMachineException("bad temperature")
            self._heat(heater, t, wait)
        elif c == 'M105':  # get temperature
            try:
                et = hal.get_extruder_temperature()
            except (IOError, OSError):
                et = None
            try:
                bt = hal.get_bed_temperature()
            except (IOError, OSError):
                bt = None
            if et is None and bt is None:
                raise GMachineException("can not measure temperature")
            answer = "E:{} B:{}".format(et, bt)
        elif c == 'M106':  # fan control
            if gcode.get('S', 1) != 0:
                self._fan(True)
            else:
                self._fan(False)
        elif c == 'M107':  # turn off fan
            self._fan(False)
        elif c == 'M111':  # enable debug
            logging_config.debug_enable()
        elif c == 'M114':  # get current position
            hal.join()
            p = self.position()
            answer = "X:{} Y:{} Z:{} E:{}".format(p.x, p.y, p.z, p.e)
        elif c is None:  # command not specified(for example, just F was passed)
            pass
        else:
            raise GMachineException("unknown command")
        # save parameters on success
        self._velocity = velocity
        self._pause = pause
        logging.debug("position {}".format(self._position))
        return answer
