# coding=utf-8
from __future__ import absolute_import

__author__ = "Shawn Bruce <kantlivelong@gmail.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2017 Shawn Bruce - Released under terms of the AGPLv3 License"

import octoprint.plugin
from octoprint.events import Events
import time
import subprocess
import threading
import glob
from flask import make_response, jsonify
from flask_babel import gettext
import periphery

try:
    from octoprint.access.permissions import Permissions
except:
    from octoprint.server import user_permission

try:
    from octoprint.util import ResettableTimer
except:
    from .util import ResettableTimer


class PSUControl(octoprint.plugin.StartupPlugin,
                 octoprint.plugin.TemplatePlugin,
                 octoprint.plugin.AssetPlugin,
                 octoprint.plugin.SettingsPlugin,
                 octoprint.plugin.SimpleApiPlugin,
                 octoprint.plugin.EventHandlerPlugin,
                 octoprint.plugin.WizardPlugin):

    def __init__(self):
        self._sub_plugins = dict()
        self._availableGPIODevices = self.get_gpio_devs()

        self.config = dict()

        self._autoOnTriggerGCodeCommandsArray = []
        self._idleIgnoreCommandsArray = []
        self._check_psu_state_thread = None
        self._check_psu_state_event = threading.Event()
        self._idleTimer = None
        self._waitForHeaters = False
        self._skipIdleTimer = False
        self._configuredGPIOPins = {}
        self._noSensing_isPSUOn = False
        self.isPSUOn = False


    def get_settings_defaults(self):
        return dict(
            GPIODevice = '',
            switchingMethod = 'GCODE',
            onoffGPIOPin = 0,
            invertonoffGPIOPin = False,
            onGCodeCommand = 'M80',
            offGCodeCommand = 'M81',
            onSysCommand = '',
            offSysCommand = '',
            switchingPlugin = '',
            enablePseudoOnOff = False,
            pseudoOnGCodeCommand = 'M80',
            pseudoOffGCodeCommand = 'M81',
            postOnDelay = 0.0,
            connectOnPowerOn = False,
            disconnectOnPowerOff = False,
            sensingMethod = 'INTERNAL',
            senseGPIOPin = 0,
            sensePollingInterval = 5,
            invertsenseGPIOPin = False,
            senseGPIOPinPUD = '',
            senseSystemCommand = '',
            sensingPlugin = '',
            autoOn = False,
            autoOnTriggerGCodeCommands = "G0,G1,G2,G3,G10,G11,G28,G29,G32,M104,M106,M109,M140,M190",
            enablePowerOffWarningDialog = True,
            powerOffWhenIdle = False,
            idleTimeout = 30,
            idleIgnoreCommands = 'M105',
            idleTimeoutWaitTemp = 50
        )


    def on_settings_initialized(self):
        scripts = self._settings.listScripts("gcode")

        if not "psucontrol_post_on" in scripts:
            self._settings.saveScript("gcode", "psucontrol_post_on", u'')

        if not "psucontrol_pre_off" in scripts:
            self._settings.saveScript("gcode", "psucontrol_pre_off", u'')

        self.reload_settings()


    def reload_settings(self):
        for k, v in self.get_settings_defaults().items():
            if isinstance(v, str):
                v = self._settings.get([k])
            elif isinstance(v, int):
                v = self._settings.get_int([k])
            elif isinstance(v, float):
                v = self._settings.get_float([k])
            elif isinstance(v, bool):
                v = self._settings.get_boolean([k])

            self.config[k] = v
            self._logger.debug("{}: {}".format(k, v))

        if self.config['enablePseudoOnOff'] and self.config['switchingMethod'] == 'GCODE':
            self._logger.warning("Pseudo On/Off cannot be used in conjunction with GCODE switching. Disabling.")
            self.config['enablePseudoOnOff'] = False

        self._autoOnTriggerGCodeCommandsArray = self.config['autoOnTriggerGCodeCommands'].split(',')
        self._idleIgnoreCommandsArray = self.config['idleIgnoreCommands'].split(',')


    def on_after_startup(self):
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO':
            self.configure_gpio()
        elif self.config['switchingMethod'] == 'PLUGIN' or self.config['sensingMethod'] == 'PLUGIN':
            if (self.config['switchingMethod'] == 'PLUGIN' and self.config['sensingMethod'] == 'PLUGIN' and
                self.config['switchingPlugin'] == self.config['sensingPlugin']):
                    self.setup_sub_plugin(self.config['switchingPlugin'])
            else:
                if self.config['switchingMethod'] == 'PLUGIN':
                    self.setup_sub_plugin(self.config['switchingPlugin'])

                if self.config['sensingMethod'] == 'PLUGIN':
                    self.setup_sub_plugin(self.config['sensingPlugin'])

        self._check_psu_state_thread = threading.Thread(target=self._check_psu_state)
        self._check_psu_state_thread.daemon = True
        self._check_psu_state_thread.start()

        self._start_idle_timer()


    def get_gpio_devs(self):
        return sorted(glob.glob('/dev/gpiochip*'))


    def cleanup_gpio(self):
        for pin in self._configuredGPIOPins.values():
            self._logger.debug("Cleaning up pin {}".format(pin))
            try:
                pin.close()
            except Exception as e:
                self._logger.error(e)
        self._configuredGPIOPins = {}


    def configure_gpio(self):
        self.cleanup_gpio()

        if self.config['switchingMethod'] == 'GPIO':
            self._logger.info("Using GPIO for On/Off")
            self._logger.info("Configuring GPIO for pin {}".format(self.config['onoffGPIOPin']))

            if not self.config['invertonoffGPIOPin']:
                initial_output = 'low'
            else:
                initial_output = 'high'

            try:
                pin = periphery.GPIO(self.config['GPIODevice'], self.config['onoffGPIOPin'], initial_output)
                self._configuredGPIOPins['switch'] = pin
            except Exception as e:
                self._logger.error(e)

        if self.config['sensingMethod'] == 'GPIO':
            self._logger.info("Using GPIO sensing to determine PSU on/off state.")
            self._logger.info("Configuring GPIO for pin {}".format(self.config['senseGPIOPin']))

            if self.config['senseGPIOPinPUD'] == 'PULL_UP':
                bias = "pull_up"
            elif self.config['senseGPIOPinPUD'] == 'PULL_DOWN':
                bias = "pull_down"
            else:
                bias = "disable"

            try:
                pin = periphery.CdevGPIO(path=self.config['GPIODevice'], line=self.config['senseGPIOPin'], direction='in', bias=bias)
                self._configuredGPIOPins['sense'] = pin
            except Exception as e:
                self._logger.error(e)


    def cleanup_sub_plugin(self, plugin_id):
        if plugin_id in self._sub_plugins:
            if not hasattr(self._sub_plugins[plugin_id], 'cleanup'):
                self._logger.error('Plugin {} does not define a cleanup method.'.format(p))
                return
            else:
                try:
                    self._sub_plugins[plugin_id].cleanup()
                except Exception as e:
                    self._logger.error(e)


    def setup_sub_plugin(self, plugin_id):
        if plugin_id in self._sub_plugins:
            if not hasattr(self._sub_plugins[plugin_id], 'setup'):
                self._logger.error('Plugin {} does not define a setup method.'.format(p))
                return
            else:
                try:
                    self._sub_plugins[plugin_id].setup()
                except Exception as e:
                    self._logger.error(e)


    def _get_plugin_key(self, implementation):
        for k, v in self._plugin_manager.plugin_implementations.items():
            if v == implementation:
                return k


    def register_plugin(self, implementation):
        k = self._get_plugin_key(implementation)

        self._logger.debug("Registering plugin - {}".format(k))

        if k not in self._sub_plugins:
            self._logger.info("Registered plugin - {}".format(k))
            self._sub_plugins[k] = implementation


    def check_psu_state(self):
        self._check_psu_state_event.set()


    def _check_psu_state(self):
        while True:
            old_isPSUOn = self.isPSUOn

            self._logger.debug("Polling PSU state...")

            if self.config['sensingMethod'] == 'GPIO':
                r = 0
                try:
                    r = self._configuredGPIOPins['sense'].read()
                except Exception as e:
                    self._logger.error(e)

                self._logger.debug("Result: {}".format(r))

                new_isPSUOn = r ^ self.config['invertsenseGPIOPin']

                self.isPSUOn = new_isPSUOn
            elif self.config['sensingMethod'] == 'SYSTEM':
                new_isPSUOn = False

                p = subprocess.Popen(self.config['senseSystemCommand'], shell=True)
                self._logger.debug("Sensing system command executed. PID={}, Command={}".format(p.pid, self.config['senseSystemCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode
                self._logger.debug("Sensing system command returned: {}".format(r))

                if r == 0:
                    new_isPSUOn = True
                elif r == 1:
                    new_isPSUOn = False

                self.isPSUOn = new_isPSUOn
            elif self.config['sensingMethod'] == 'INTERNAL':
                self.isPSUOn = self._noSensing_isPSUOn
            elif self.config['sensingMethod'] == 'PLUGIN':
                p = self.config['sensingPlugin']

                r = False

                if not hasattr(self._sub_plugins[p], 'get_psu_state'):
                    self._logger.error('Plugin {} is configured for sensing but get_psu_state is not defined.'.format(p))
                else:
                    try:
                        r = self._sub_plugins[p].get_psu_state()
                    except Exception as e:
                        self._logger.error(e)

                self.isPSUOn = r
            else:
                self.isPSUOn = False

            self._logger.debug("isPSUOn: {}".format(self.isPSUOn))

            if (old_isPSUOn != self.isPSUOn):
                self._logger.debug("PSU state changed, firing psu_state_changed event.")

                event = Events.PLUGIN_PSUCONTROL_PSU_STATE_CHANGED
                self._event_bus.fire(event, payload=dict(isPSUOn=self.isPSUOn))

            if (old_isPSUOn != self.isPSUOn) and self.isPSUOn:
                self._start_idle_timer()
            elif (old_isPSUOn != self.isPSUOn) and not self.isPSUOn:
                self._stop_idle_timer()

            self._plugin_manager.send_plugin_message(self._identifier, dict(isPSUOn=self.isPSUOn))

            self._check_psu_state_event.wait(self.config['sensePollingInterval'])
            self._check_psu_state_event.clear()


    def _start_idle_timer(self):
        self._stop_idle_timer()

        if self.config['powerOffWhenIdle'] and self.isPSUOn:
            self._idleTimer = ResettableTimer(self.config['idleTimeout'] * 60, self._idle_poweroff)
            self._idleTimer.start()


    def _stop_idle_timer(self):
        if self._idleTimer:
            self._idleTimer.cancel()
            self._idleTimer = None


    def _reset_idle_timer(self):
        try:
            if self._idleTimer.is_alive():
                self._idleTimer.reset()
            else:
                raise Exception()
        except:
            self._start_idle_timer()


    def _idle_poweroff(self):
        if not self.config['powerOffWhenIdle']:
            return

        if self._waitForHeaters:
            return

        if self._printer.is_printing() or self._printer.is_paused():
            return

        self._logger.info("Idle timeout reached after {} minute(s). Turning heaters off prior to shutting off PSU.".format(self.config['idleTimeout']))
        if self._wait_for_heaters():
            self._logger.info("Heaters below temperature.")
            self.turn_psu_off()
        else:
            self._logger.info("Aborted PSU shut down due to activity.")


    def _wait_for_heaters(self):
        self._waitForHeaters = True
        heaters = self._printer.get_current_temperatures()

        for heater, entry in heaters.items():
            target = entry.get("target")
            if target is None:
                # heater doesn't exist in fw
                continue

            try:
                temp = float(target)
            except ValueError:
                # not a float for some reason, skip it
                continue

            if temp != 0:
                self._logger.info("Turning off heater: {}".format(heater))
                self._skipIdleTimer = True
                self._printer.set_temperature(heater, 0)
                self._skipIdleTimer = False
            else:
                self._logger.debug("Heater {} already off.".format(heater))

        while True:
            if not self._waitForHeaters:
                return False

            heaters = self._printer.get_current_temperatures()

            highest_temp = 0
            heaters_above_waittemp = []
            for heater, entry in heaters.items():
                if not heater.startswith("tool"):
                    continue

                actual = entry.get("actual")
                if actual is None:
                    # heater doesn't exist in fw
                    continue

                try:
                    temp = float(actual)
                except ValueError:
                    # not a float for some reason, skip it
                    continue

                self._logger.debug("Heater {} = {}C".format(heater, temp))
                if temp > self.config['idleTimeoutWaitTemp']:
                    heaters_above_waittemp.append(heater)

                if temp > highest_temp:
                    highest_temp = temp

            if highest_temp <= self.config['idleTimeoutWaitTemp']:
                self._waitForHeaters = False
                return True

            self._logger.info("Waiting for heaters({}) before shutting off PSU...".format(', '.join(heaters_above_waittemp)))
            time.sleep(5)


    def hook_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        skipQueuing = False

        if not gcode:
            gcode = cmd.split(' ', 1)[0]

        if self.config['enablePseudoOnOff']:
            if gcode == self.config['pseudoOnGCodeCommand']:
                self.turn_psu_on()
                comm_instance._log("PSUControl: ok")
                skipQueuing = True
            elif gcode == self.config['pseudoOffGCodeCommand']:
                self.turn_psu_off()
                comm_instance._log("PSUControl: ok")
                skipQueuing = True

        if (not self.isPSUOn and self.config['autoOn'] and (gcode in self._autoOnTriggerGCodeCommandsArray)):
            self._logger.info("Auto-On - Turning PSU On (Triggered by {})".format(gcode))
            self.turn_psu_on()

        if self.config['powerOffWhenIdle'] and self.isPSUOn and not self._skipIdleTimer:
            if not (gcode in self._idleIgnoreCommandsArray):
                self._waitForHeaters = False
                self._reset_idle_timer()

        if skipQueuing:
            return (None,)


    def turn_psu_on(self):
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            self._logger.info("Switching PSU On")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching PSU On Using GCODE: {}".format(self.config['onGCodeCommand']))
                self._printer.commands(self.config['onGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching PSU On Using SYSTEM: {}".format(self.config['onSysCommand']))

                p = subprocess.Popen(self.config['onSysCommand'], shell=True)
                self._logger.debug("On system command executed. PID={}, Command={}".format(p.pid, self.config['onSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("On system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                self._logger.debug("Switching PSU On Using GPIO: {}".format(self.config['onoffGPIOPin']))
                pin_output = bool(1 ^ self.config['invertonoffGPIOPin'])

                try:
                    self._configuredGPIOPins['switch'].write(pin_output)
                except Exception as e:
                    self._logger.error(e)
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching PSU On Using PLUGIN: {}".format(p))

                if not hasattr(self._sub_plugins[p], 'turn_psu_on'):
                    self._logger.error('Plugin {} is configured for switching but turn_psu_on is not defined.'.format(p))
                    return

                try:
                    r = self._sub_plugins[p].turn_psu_on()
                except Exception as e:
                    self._logger.error(e)
                    return

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isPSUOn = True

            time.sleep(0.1 + self.config['postOnDelay'])

            self.check_psu_state()

            if self.config['connectOnPowerOn'] and self._printer.is_closed_or_error():
                self._printer.connect()
                time.sleep(0.1)

            if not self._printer.is_closed_or_error():
                self._printer.script("psucontrol_post_on", must_be_set=False)


    def turn_psu_off(self):
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            if not self._printer.is_closed_or_error():
                self._printer.script("psucontrol_pre_off", must_be_set=False)

            self._logger.info("Switching PSU Off")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching PSU Off Using GCODE: {}".format(self.config['offGCodeCommand']))
                self._printer.commands(self.config['offGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching PSU Off Using SYSTEM: {}".format(self.config['offSysCommand']))

                p = subprocess.Popen(self.config['offSysCommand'], shell=True)
                self._logger.debug("Off system command executed. PID={}, Command={}".format(p.pid, self.config['offSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("Off system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                self._logger.debug("Switching PSU Off Using GPIO: {}".format(self.config['onoffGPIOPin']))
                pin_output = bool(0 ^ self.config['invertonoffGPIOPin'])

                try:
                    self._configuredGPIOPins['switch'].write(pin_output)
                except Exception as e:
                    self._logger.error(e)
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching PSU Off Using PLUGIN: {}".format(p))

                if not hasattr(self._sub_plugins[p], 'turn_psu_off'):
                    self._logger.error('Plugin {} is configured for switching but turn_psu_off is not defined.'.format(p))
                    return

                try:
                    r = self._sub_plugins[p].turn_psu_off()
                except Exception as e:
                    self._logger.error(e)
                    return

            if self.config['disconnectOnPowerOff']:
                self._printer.disconnect()

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isPSUOn = False

            time.sleep(0.1)
            self.check_psu_state()


    def get_psu_state(self):
        return self.isPSUOn


    def on_event(self, event, payload):
        if event == Events.CLIENT_OPENED:
            p = []
            for k in list(self._sub_plugins.keys()):
                p.append(dict(Id=k, displayName=self._plugin_manager.plugins[k].name))

            d = dict(isPSUOn=self.isPSUOn,
                     availableGPIODevices=self._availableGPIODevices,
                     availablePlugins=p
                )

            self._plugin_manager.send_plugin_message(self._identifier, d)
            return


    def get_api_commands(self):
        return dict(
            turnPSUOn=[],
            turnPSUOff=[],
            togglePSU=[],
            getPSUState=[]
        )


    def on_api_get(self, request):
        return self.on_api_command("getPSUState", [])


    def on_api_command(self, command, data):
        if command in ['turnPSUOn', 'turnPSUOff', 'togglePSU']:
            try:
                if not Permissions.PLUGIN_PSUCONTROL_CONTROL.can():
                    return make_response("Insufficient rights", 403)
            except:
                if not user_permission.can():
                    return make_response("Insufficient rights", 403)
        elif command in ['getPSUState']:
            try:
                if not Permissions.STATUS.can():
                    return make_response("Insufficient rights", 403)
            except:
                if not user_permission.can():
                    return make_response("Insufficient rights", 403)

        if command == 'turnPSUOn':
            self.turn_psu_on()
        elif command == 'turnPSUOff':
            self.turn_psu_off()
        elif command == 'togglePSU':
            if self.isPSUOn:
                self.turn_psu_off()
            else:
                self.turn_psu_on()
        elif command == 'getPSUState':
            return jsonify(isPSUOn=self.isPSUOn)


    def on_settings_save(self, data):
        if 'scripts_gcode_psucontrol_post_on' in data:
            script = data["scripts_gcode_psucontrol_post_on"]
            self._settings.saveScript("gcode", "psucontrol_post_on", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_psucontrol_post_on')

        if 'scripts_gcode_psucontrol_pre_off' in data:
            script = data["scripts_gcode_psucontrol_pre_off"]
            self._settings.saveScript("gcode", "psucontrol_pre_off", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_psucontrol_pre_off')

        old_config = self.config.copy()

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        self.reload_settings()

        #GCode switching and PseudoOnOff are not compatible.
        if self.config['switchingMethod'] == 'GCODE' and self.config['enablePseudoOnOff']:
            self.config['enablePseudoOnOff'] = False
            self._settings.set_boolean(["enablePseudoOnOff"], False)
            self._settings.save()


        #cleanup GPIO
        self.cleanup_gpio()

        #cleanup sub plugins
        for k in self._sub_plugins.keys():
            self.cleanup_sub_plugin(k)

        #configure GPIO
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO':
            self.configure_gpio()

        #configure sub plugins
        if self.config['switchingMethod'] == 'PLUGIN' or self.config['sensingMethod'] == 'PLUGIN':
            if (self.config['switchingMethod'] == 'PLUGIN' and self.config['sensingMethod'] == 'PLUGIN' and
                self.config['switchingPlugin'] == self.config['sensingPlugin']):
                    self.setup_sub_plugin(self.config['switchingPlugin'])
            else:
                if self.config['switchingMethod'] == 'PLUGIN':
                    self.setup_sub_plugin(self.config['switchingPlugin'])

                if self.config['sensingMethod'] == 'PLUGIN':
                    self.setup_sub_plugin(self.config['sensingPlugin'])

        self._start_idle_timer()


    def get_wizard_version(self):
        return 1


    def is_wizard_required(self):
        return True


    def get_settings_version(self):
        return 4


    def on_settings_migrate(self, target, current=None):
        if current is None:
            current = 0

        if current < 2:
            # v2 changes names of settings variables to accomidate system commands.
            cur_switchingMethod = self._settings.get(["switchingMethod"])
            if cur_switchingMethod is not None and cur_switchingMethod == "COMMAND":
                self._logger.info("Migrating Setting: switchingMethod=COMMAND -> switchingMethod=GCODE")
                self._settings.set(["switchingMethod"], "GCODE")

            cur_onCommand = self._settings.get(["onCommand"])
            if cur_onCommand is not None:
                self._logger.info("Migrating Setting: onCommand={0} -> onGCodeCommand={0}".format(cur_onCommand))
                self._settings.set(["onGCodeCommand"], cur_onCommand)
                self._settings.remove(["onCommand"])
            
            cur_offCommand = self._settings.get(["offCommand"])
            if cur_offCommand is not None:
                self._logger.info("Migrating Setting: offCommand={0} -> offGCodeCommand={0}".format(cur_offCommand))
                self._settings.set(["offGCodeCommand"], cur_offCommand)
                self._settings.remove(["offCommand"])

            cur_autoOnCommands = self._settings.get(["autoOnCommands"])
            if cur_autoOnCommands is not None:
                self._logger.info("Migrating Setting: autoOnCommands={0} -> autoOnTriggerGCodeCommands={0}".format(cur_autoOnCommands))
                self._settings.set(["autoOnTriggerGCodeCommands"], cur_autoOnCommands)
                self._settings.remove(["autoOnCommands"])

        if current < 3:
            # v3 adds support for multiple sensing methods
            cur_enableSensing = self._settings.get_boolean(["enableSensing"])
            if cur_enableSensing is not None and cur_enableSensing:
                self._logger.info("Migrating Setting: enableSensing=True -> sensingMethod=GPIO")
                self._settings.set(["sensingMethod"], "GPIO")
                self._settings.remove(["enableSensing"])

        if current < 4:
            # v4 drops RPi.GPIO in favor of Python-Periphery.
            cur_GPIOMode = self._settings.get(["GPIOMode"])
            cur_switchingMethod = self._settings.get(["switchingMethod"])
            cur_sensingMethod = self._settings.get(["sensingMethod"])
            cur_onoffGPIOPin = self._settings.get_int(["onoffGPIOPin"])
            cur_invertonoffGPIOPin = self._settings.get_boolean(["invertonoffGPIOPin"])
            cur_senseGPIOPin = self._settings.get_int(["senseGPIOPin"])
            cur_invertsenseGPIOPin = self._settings.get_boolean(["invertsenseGPIOPin"])
            cur_senseGPIOPinPUD = self._settings.get(["senseGPIOPinPUD"])

            if (cur_switchingMethod == 'GPIO' or cur_sensingMethod == 'GPIO') and cur_GPIOMode == 'BOARD':
                # Convert BOARD pin numbers to BCM

                try:
                    global GPIO
                    import RPi.GPIO as GPIO
                    _has_gpio = True
                except (ImportError, RuntimeError):
                    _has_gpio = False

                if _has_gpio:
                    def _gpio_board_to_bcm(pin):
                        _pin_to_gpio_rev1 = [-1, -1, -1, 0, -1, 1, -1, 4, 14, -1, 15, 17, 18, 21, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
                        _pin_to_gpio_rev2 = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, 17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
                        _pin_to_gpio_rev3 = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, 17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, 5, -1, 6, 12, 13, -1, 19, 16, 26, 20, -1, 21 ]

                        if GPIO.RPI_REVISION == 1:
                            pin_to_gpio = _pin_to_gpio_rev1
                        elif GPIO.RPI_REVISION == 2:
                            pin_to_gpio = _pin_to_gpio_rev2
                        else:
                            pin_to_gpio = _pin_to_gpio_rev3

                        return pin_to_gpio[pin]

                    if cur_switchingMethod == 'GPIO':
                        p = _gpio_board_to_bcm(cur_onoffGPIOPin)
                        self._logger.info("Converting pin number from BOARD to BCM. onoffGPIOPin={} -> onoffGPIOPin={}".format(cur_onoffGPIOPin, p))
                        self._settings.set_int(["onoffGPIOPin"], p)

                    if cur_sensingMethod == 'GPIO':
                        p = _gpio_board_to_bcm(cur_senseGPIOPin)
                        self._logger.info("Converting pin number from BOARD to BCM. senseGPIOPin={} -> senseGPIOPin={}".format(cur_senseGPIOPin, p))
                        self._settings.set_int(["senseGPIOPin"], p)

                if len(self._availableGPIODevices) > 0:
                    # This was likely a Raspberry Pi using RPi.GPIO. Set GPIODevice to the first dev found which is likely /dev/gpiochip0
                    self._logger.info("Setting GPIODevice to the first found. GPIODevice={}".format(self._availableGPIODevices[0]))
                    self._settings.set(["GPIODevice"], self._availableGPIODevices[0])
                else:
                    # GPIO was used for either but no GPIO devices exist. Reset to defaults.
                    self._logger.warning("No GPIO devices found. Reverting switchingMethod and sensingMethod to defaults.")
                    self._settings.remove(["switchingMethod"])
                    self._settings.remove(["sensingMethod"])


                # Write the config to psucontrol_rpigpio just in case the user decides/needs to switch to it.
                self._logger.info("Writing original GPIO related settings to psucontrol_rpigpio.")
                self._settings.global_set(['plugins', 'psucontrol_rpigpio', 'GPIOMode'], cur_GPIOMode)
                self._settings.global_set(['plugins', 'psucontrol_rpigpio', 'switchingMethod'], cur_switchingMethod)
                self._settings.global_set(['plugins', 'psucontrol_rpigpio', 'sensingMethod'], cur_sensingMethod)
                self._settings.global_set_int(['plugins', 'psucontrol_rpigpio', 'onoffGPIOPin'], cur_onoffGPIOPin)
                self._settings.global_set_boolean(['plugins', 'psucontrol_rpigpio', 'invertonoffGPIOPin'], cur_invertonoffGPIOPin)
                self._settings.global_set_int(['plugins', 'psucontrol_rpigpio', 'senseGPIOPin'], cur_senseGPIOPin)
                self._settings.global_set_boolean(['plugins', 'psucontrol_rpigpio', 'invertsenseGPIOPin'], cur_invertsenseGPIOPin)
                self._settings.global_set(['plugins', 'psucontrol_rpigpio', 'senseGPIOPinPUD'], cur_senseGPIOPinPUD)

            # Remove now unused config option
            self._logger.info("Removing Setting: GPIOMode")
            self._settings.remove(["GPIOMode"])


    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=True)
        ]


    def get_assets(self):
        return {
            "js": ["js/psucontrol.js"],
            "less": ["less/psucontrol.less"],
            "css": ["css/psucontrol.min.css"]

        } 


    def get_update_information(self):
        return dict(
            psucontrol=dict(
                displayName="PSU Control",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="kantlivelong",
                repo="OctoPrint-PSUControl",
                current=self._plugin_version,

                # update method: pip w/ dependency links
                pip="https://github.com/kantlivelong/OctoPrint-PSUControl/archive/{target_version}.zip"
            )
        )


    def register_custom_events(self):
        return ["psu_state_changed"]


    def get_additional_permissions(self, *args, **kwargs):
        return [
            dict(key="CONTROL",
                 name="Control",
                 description=gettext("Allows switching PSU on/off"),
                 roles=["admin"],
                 dangerous=True,
                 default_groups=[Permissions.ADMIN_GROUP])
        ]


    def cli_commands(self, cli_group, pass_octoprint_ctx, *args, **kwargs):
        # Requires OctoPrint >= 1.3.5
        import click
        import sys
        import json
        import requests.exceptions
        from octoprint.cli.client import create_client, client_options

        def _api_command(command, apikey, host, port, httpuser, httppass, https, prefix):
            if prefix == None:
                prefix = '/api'

            client = create_client(settings=cli_group.settings,
                                   apikey=apikey,
                                   host=host,
                                   port=port,
                                   httpuser=httpuser,
                                   httppass=httppass,
                                   https=https,
                                   prefix=prefix)

            r = client.post_command("plugin/psucontrol", command)
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                click.echo("HTTP Error, got {}".format(e))
                sys.exit(1)

            return r

        @client_options
        @click.command("on")
        def turnPSUOn_command(apikey, host, port, httpuser, httppass, https, prefix):
            """Turn the PSU On"""
            r = _api_command('turnPSUOn', apikey, host, port, httpuser, httppass, https, prefix)

            if r.status_code in [200, 204]:
                click.echo('ok')

        @client_options
        @click.command("off")
        def turnPSUOff_command(apikey, host, port, httpuser, httppass, https, prefix):
            """Turn the PSU Off"""
            r = _api_command('turnPSUOff', apikey, host, port, httpuser, httppass, https, prefix)

            if r.status_code in [200, 204]:
                click.echo('ok')

        @client_options
        @click.command("toggle")
        def togglePSU_command(apikey, host, port, httpuser, httppass, https, prefix):
            """Toggle the PSU On/Off"""
            r = _api_command('togglePSU', apikey, host, port, httpuser, httppass, https, prefix)

            if r.status_code in [200, 204]:
                click.echo('ok')

        @click.option("--return-int", is_flag=True, help="Return the PSU state as a boolean integer.")
        @client_options
        @click.command("status")
        def getPSUState_command(return_int, apikey, host, port, httpuser, httppass, https, prefix):
            """Get the current PSU status"""
            r = _api_command('getPSUState', apikey, host, port, httpuser, httppass, https, prefix)

            if r.status_code in [200, 204]:
                data = json.loads(r._content)

                if return_int:
                    click.echo(int(data['isPSUOn']))
                else:
                    if data['isPSUOn']:
                        click.echo('on')
                    else:
                        click.echo('off')

        return [turnPSUOn_command, turnPSUOff_command, togglePSU_command, getPSUState_command]


__plugin_name__ = "PSU Control"
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PSUControl()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.hook_gcode_queuing,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.events.register_custom_events": __plugin_implementation__.register_custom_events,
        "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
        "octoprint.cli.commands": __plugin_implementation__.cli_commands
    }

    global __plugin_helpers__
    __plugin_helpers__ = dict(
        get_psu_state = __plugin_implementation__.get_psu_state,
        turn_psu_on = __plugin_implementation__.turn_psu_on,
        turn_psu_off = __plugin_implementation__.turn_psu_off,
        register_plugin = __plugin_implementation__.register_plugin
    )
