# Wirepas Oy licensed under Apache License, Version 2.0
#
# See file LICENSE for full license details.
#


import logging
from wirepas_messaging.gateway.api import (
    GatewayResultCode,
    ScratchpadStatus,
    ScratchpadType,
)
from gi.repository import GLib
from .return_code import ReturnCode


DBUS_SINK_PREFIX = "com.wirepas.sink."


class Sink(object):
    def __init__(
        self, bus, proxy, sink_id, unique_name, on_stack_started, logger=None, **kwargs
    ):

        self.proxy = proxy
        self.sink_id = sink_id
        self.network_address = None
        self.on_stack_started = on_stack_started
        self.bus = bus
        self.unique_name = unique_name
        self.on_started_handle = None

        self.logger = logger or logging.getLogger(__name__)

    def register_for_stack_started(self):
        # Use the subscribe directly to be able to specify the sender
        self.on_started_handle = self.bus.subscribe(
            signal="StackStarted",
            object="/com/wirepas/sink",
            iface="com.wirepas.sink.config1",
            sender=self.unique_name,
            signal_fired=self._on_stack_started,
        )

    def unregister_from_stack_started(self):
        if self.on_started_handle is not None:
            self.on_started_handle.unsubscribe()

    def get_network_address(self, force=False):
        if self.network_address is None or force:
            # Network address is not known or must be updated
            try:
                self.network_address = self.proxy.NetworkAddress
            except GLib.Error:
                self.logger.exception("Could not get network address")
                pass

        return self.network_address

    def send_data(
        self,
        dst,
        src_ep,
        dst_ep,
        qos,
        initial_time,
        data,
        is_unack_csma_ca=False,
        hop_limit=0,
    ):
        try:
            res = self.proxy.SendMessage(
                dst,
                src_ep,
                dst_ep,
                initial_time,
                qos,
                is_unack_csma_ca,
                hop_limit,
                data,
            )
            if res != 0:
                self.logger.error("Cannot send message err={}\n".format(res))
                return ReturnCode.error_from_dbus_return_code(res)
        except GLib.Error as e:
            self.logger.exception("Fail to send message: {}".format(e.message))
            return ReturnCode.error_from_dbus_exception(e.message)

        return GatewayResultCode.GW_RES_OK

    def _on_stack_started(self, sender, object, iface, signal, params):
        # Force update of network address in case remote api modify it
        self.get_network_address(True)

        self.on_stack_started(self.sink_id)

    def _get_param(self, dic, key, attribute):
        try:
            dic[key] = getattr(self.proxy, attribute)
        except GLib.Error:
            # Exception raised when getting attribute (probably not set)
            # Discard channel_map as parameter present only for old stacks
            if key != "channel_map":
                # Warning and not an error as normal behavior if not set
                self.logger.warning("Cannot get {} in config (is it set?)".format(key))

    def _get_pair_params(self, dic, key1, att1, key2, att2):
        # Some settings are only relevant if the a both can be retrieved
        try:
            att1_val = getattr(self.proxy, att1)
            att2_val = getattr(self.proxy, att2)
        except GLib.Error:
            self.logger.debug(
                "Cannot get one of the pair value ({}-{})".format(key1, key2)
            )
            return None

        dic[key1] = att1_val
        dic[key2] = att2_val

    def read_config(self):
        config = {}
        config["sink_id"] = self.sink_id

        # Should always be available
        try:
            config["started"] = (self.proxy.StackStatus & 0x01) == 0
        except GLib.Error as e:
            error = ReturnCode.error_from_dbus_exception(e.message)
            self.logger.exception("Cannot get Stack state: {}".format(error))
            return None

        self._get_param(config, "node_address", "NodeAddress")
        self._get_param(config, "node_role", "NodeRole")
        self._get_param(config, "network_address", "NetworkAddress")
        self._get_param(config, "network_channel", "NetworkChannel")
        self._get_param(config, "channel_map", "ChannelMap")
        self._get_pair_params(config, "max_ac", "ACRangeMax", "min_ac", "ACRangeMin")
        self._get_pair_params(
            config, "max_ac_cur", "ACRangeMaxCur", "min_ac_cur", "ACRangeMinCur"
        )
        self._get_pair_params(config, "max_ch", "ChRangeMax", "min_ch", "ChRangeMin")
        self._get_param(config, "max_mtu", "MaxMtu")
        self._get_param(config, "hw_magic", "HwMagic")
        self._get_param(config, "stack_profile", "StackProfile")
        self._get_param(config, "firmware_version", "FirmwareVersion")
        self._get_param(config, "app_config_max_size", "AppConfigMaxSize")

        try:
            are_keys_set = self.proxy.AuthenticationKeySet and self.proxy.CipherKeySet

            config["are_keys_set"] = are_keys_set
        except GLib.Error:
            self.logger.error("Cannot get key status")

        try:
            seq, diag, data = self.proxy.GetAppConfig()
            config["app_config_seq"] = seq
            config["app_config_diag"] = diag
            config["app_config_data"] = bytearray(data)
        except GLib.Error:
            # If node is blank it is not a sink
            # so app config cannot be accessed
            self.logger.warning("Cannot get App Config")

        return config

    def _set_param(self, dic, key, attribute):
        try:
            value = dic[key]
            # Stop the stack if not already stopped
            if self.proxy.StackStatus == 0:
                self.proxy.SetStackState(False)

            setattr(self.proxy, attribute, value)

        except KeyError:
            # key not defined in config
            self.logger.debug("key not present: {}".format(key))
            pass
        except GLib.Error as e:
            # Exception raised when setting attribute
            self.logger.error(
                "Cannot set {} for param {} on sink {}: {}\n".format(
                    value, key, self.sink_id, e.message
                )
            )
            raise RuntimeError(ReturnCode.error_from_dbus_exception(e.message))

    def write_config(self, config):
        # Should always be available
        try:
            stack_started = (self.proxy.StackStatus & 0x01) == 0
        except GLib.Error as e:
            self.logger.error("Cannot get Stack state. Problem in com probably")
            return ReturnCode.error_from_dbus_exception(e.message)

        # The write config has only one return code possible
        # so the last error code will be returned
        res = GatewayResultCode.GW_RES_OK

        # First try to set app_config as any other config will stop the stack
        try:
            seq = config["app_config_seq"]
            diag = config["app_config_diag"]
            data = config["app_config_data"]

            self.logger.info(
                "Set app config with "
                "{app_config_seq}:"
                "{app_config_diag}:"
                "{app_config_data}".format(**config)
            )

            self.proxy.SetAppConfig(seq, diag, data)
        except KeyError:
            # App config not defined in new config
            self.logger.debug("Missing key app_config key in config: {}".format(config))
            pass
        except GLib.Error as e:
            res = ReturnCode.error_from_dbus_exception(e.message)
            self.logger.exception("Cannot set App Config {}".format(e.message))

        config_to_dbus_param = dict(
            [
                ("node_address", "NodeAddress"),
                ("node_role", "NodeRole"),
                ("network_address", "NetworkAddress"),
                ("network_channel", "NetworkChannel"),
                ("channel_map", "ChannelMap"),
                ("authentication_key", "AuthenticationKey"),
                ("cipher_key", "CipherKey"),
            ]
        )

        # Any following call will stop the stack
        for param in config_to_dbus_param:
            try:
                self._set_param(config, param, config_to_dbus_param[param])
            except RuntimeError as e:
                self.logger.exception("Runtime error when setting parameter")
                res = e.args[0]

        # Set stack in state defined by new config or set it as it was
        # previously
        try:
            new_state = config["started"]
        except KeyError:
            # Not defined in config
            new_state = stack_started

        try:
            self.proxy.SetStackState(new_state)
        except GLib.Error as err:
            self.logger.exception("Cannot set Stack state. Problem in com probably")
            return ReturnCode.error_from_dbus_exception(err.message)

        # In case the network address was updated, read it back for our cached
        # value
        self.get_network_address(True)

        return res

    def get_scratchpad_status(self):
        d = {}

        dbus_to_gateway_satus = dict(
            [
                (0, ScratchpadStatus.SCRATCHPAD_STATUS_SUCCESS),
                (255, ScratchpadStatus.SCRATCHPAD_STATUS_NEW)
                # Anything else is ERROR
            ]
        )
        try:
            status = self.proxy.StoredStatus
            d["stored_status"] = dbus_to_gateway_satus[status]
        except GLib.Error:
            # Exception raised when getting attribute (probably not set)
            self.logger.error("Cannot get stored status in config\n")
        except KeyError:
            # Between 1 and 254 => Error
            self.logger.error("Scratchpad stored status has error: {}".format(status))
            d["stored_status"] = ScratchpadStatus.SCRATCHPAD_STATUS_ERROR

        dbus_to_gateway_type = dict(
            [
                (0, ScratchpadType.SCRATCHPAD_TYPE_BLANK),
                (1, ScratchpadType.SCRATCHPAD_TYPE_PRESENT),
                (2, ScratchpadType.SCRATCHPAD_TYPE_PROCESS),
            ]
        )
        try:
            type = self.proxy.StoredType
            d["stored_type"] = dbus_to_gateway_type[type]
        except GLib.Error:
            # Exception raised when getting attribute (probably not set)
            self.logger.error("Cannot get stored type in config\n")

        stored = {}
        self._get_param(stored, "seq", "StoredSeq")
        self._get_param(stored, "crc", "StoredCrc")
        self._get_param(stored, "len", "StoredLen")
        d["stored_scartchpad"] = stored

        processed = {}
        self._get_param(processed, "seq", "ProcessedSeq")
        self._get_param(processed, "crc", "ProcessedCrc")
        self._get_param(processed, "len", "ProcessedLen")
        d["processed_scartchpad"] = processed

        self._get_param(d, "firmware_area_id", "FirmwareAreaId")

        return d

    def process_scratchpad(self):
        restart = False
        try:
            # Stop the stack if not already stopped
            if self.proxy.StackStatus == 0:
                self.proxy.SetStackState(False)
                restart = True
        except GLib.Error:
            self.logger.error("Sink in invalid state")
            return GatewayResultCode.GW_RES_INVALID_SINK_STATE

        try:
            self.proxy.ProcessScratchpad()
        except GLib.Error as e:
            self.logger.error("Could not restart sink's state")
            return ReturnCode.error_from_dbus_exception(e.message)

        if restart:
            try:
                self.proxy.SetStackState(True)
            except GLib.Error:
                self.logger.debug("Sink in invalid state")
                return GatewayResultCode.GW_RES_INTERNAL_ERROR

        return GatewayResultCode.GW_RES_OK

    def upload_scratchpad(self, seq, file):
        restart = False
        try:
            # Stop the stack if not already stopped
            if self.proxy.StackStatus == 0:
                self.proxy.SetStackState(False)
                restart = True
        except GLib.Error:
            self.logger.error("Sink in invalid state")
            return GatewayResultCode.GW_RES_INVALID_SINK_STATE

        try:
            self.proxy.UploadScratchpad(seq, file)
            self.logger.info(
                "Scratchpad loaded with seq {} on sink {}".format(seq, self.sink_id)
            )
        except GLib.Error as e:
            self.logger.exception("Cannot upload local scratchpad")
            return ReturnCode.error_from_dbus_exception(e.message)

        if restart:
            try:
                # Restart sink if we stopped it for this request
                self.proxy.SetStackState(True)
            except GLib.Error:
                self.logger.error("Could not restart sink's state")
                return GatewayResultCode.GW_RES_INTERNAL_ERROR

        return GatewayResultCode.GW_RES_OK


class SinkManager(object):
    "Helper class to manage the Sink list"

    def __init__(
        self,
        bus,
        on_new_sink_cb,
        on_sink_removal_cb,
        on_stack_started,
        logger=None,
        **kwargs
    ):

        self.logger = logger or logging.getLogger(__name__)
        self.sinks = {}
        # List used to quickly retrieved sink well known name
        self.sender_to_name = {}
        self.bus = bus

        self.add_cb = None
        self.rm_cb = None
        self.stack_started_cb = on_stack_started

        bus_monitor = self.bus.get("org.freedesktop.DBus")

        # Find sinks already on bus
        for name in bus_monitor.ListNames():
            if name.startswith(DBUS_SINK_PREFIX):
                short_name = name[len(DBUS_SINK_PREFIX) :]
                self._add_sink(short_name, bus_monitor.GetNameOwner(name))

        # Monitor the bus for connections
        self.bus.subscribe(
            sender="org.freedesktop.DBus",
            signal="NameOwnerChanged",
            signal_fired=self._on_name_owner_changed,
        )

        # Set them at the end to be sure Sink Manager is ready when cb are fired
        self.add_cb = on_new_sink_cb
        self.rm_cb = on_sink_removal_cb

    def _add_sink(self, short_name, unique_name):
        if short_name in self.sinks:
            self.logger.warning("Sink already in list sink name={}".format(short_name))
            return

        # Open proxy for this sink
        proxy = self.bus.get(
            DBUS_SINK_PREFIX + short_name,  # Bus name
            "/com/wirepas/sink",  # Object path
        )

        sink = Sink(
            bus=self.bus,
            proxy=proxy,
            sink_id=short_name,
            unique_name=unique_name,
            on_stack_started=self.stack_started_cb,
            logger=self.logger,
        )

        sink.register_for_stack_started()

        self.sinks[short_name] = sink

        self.sender_to_name[unique_name] = short_name

        if self.add_cb is not None:
            self.add_cb(short_name)

        self.logger.info("New sink added with name {}".format(short_name))

    def _remove_sink(self, short_name):
        try:
            sink = self.sinks.pop(short_name)
            sink.unregister_from_stack_started()

            # Remove Sink to association list
            for k, v in self.sender_to_name.items():
                if v == short_name:
                    self.sender_to_name.pop(k)
                    self.logger.warning(
                        "Association removed from {} => {}".format(k, v)
                    )
                    break

            # call client cb
            if self.rm_cb is not None:
                self.rm_cb(short_name)
        except KeyError:
            self.logger.error("Cannot remove {} from sink list".format(short_name))

        self.logger.info("Sink removed with name {}".format(short_name))

    def _on_name_owner_changed(self, sender, object, iface, signal, params):
        well_known_name = params[0]
        if well_known_name.startswith(DBUS_SINK_PREFIX):
            short_name = well_known_name[len(DBUS_SINK_PREFIX) :]
            # Owner change on a sink, check if it is removal or addition
            old_owner = params[1]
            new_owner = params[2]
            if old_owner == "":
                # New sink connection
                self._add_sink(short_name, new_owner)
            elif new_owner == "":
                # Removal
                self._remove_sink(short_name)
            else:
                self.logger.critical(
                    "Not addition nor removal ??? {}: {} => {}".format(
                        well_known_name, old_owner, new_owner
                    )
                )

    def get_sinks(self):
        # Return a list that is a copy to avoid modification
        # of list while iterating on it (if new sink is connected)
        return list(self.sinks.values())

    def get_sink_name(self, bus_name):
        try:
            return self.sender_to_name[bus_name]
        except KeyError:
            self.logger.error("Unknown sink {} from sink list".format(bus_name))
            return None

    def get_sink(self, short_name):
        try:
            return self.sinks[short_name]
        except KeyError:
            self.logger.error("Unknown sink {} from sink list".format(short_name))
            return None