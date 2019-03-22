from threading import RLock

from ieee802dot1qcc.common import InterfaceID
from ieee802dot1qcc.listener import Listener
from ieee802dot1qcc.status import Status, StatusInfo, TalkerStatus, ListenerStatus, FailureCode
from ieee802dot1qcc.talker import Talker
from ieee802dot1qcc.dataframespec import IEEE802MacAddresses, IEEE802VlanTag, IPv4Tuple, IPv6Tuple, PROTOCOL_TCP, \
    PROTOCOL_UDP, UncheckedIPv4Tuple

from odl_client.irt_odlclient.stream import IRTMultiStream, IRTPartialStream
from odl_client.base_odlclient.openflow.match import BaseMatch

class QccMatch(BaseMatch):
    """
    Translate a Qcc Frame Specification into a BaseMatch.
    """

    @classmethod
    def from_framespec(cls, frame_spec):
        if isinstance(frame_spec, IEEE802MacAddresses):
            fields = {}

            if frame_spec.source_mac_address is not None and frame_spec.source_mac_address != IEEE802MacAddresses.ANY_MAC_ADR:
                fields["mac_source_address"] = frame_spec.source_mac_address

            if frame_spec.destination_mac_address is not None and frame_spec.destination_mac_address != IEEE802MacAddresses.ANY_MAC_ADR:
                fields["mac_destination_address"] = frame_spec.destination_mac_address

            return BaseMatch(**fields)

        elif isinstance(frame_spec, IEEE802VlanTag):
            raise NotImplementedError()  # fixme

        elif isinstance(frame_spec, IPv4Tuple) or isinstance(frame_spec, UncheckedIPv4Tuple):
            fields = {}

            if isinstance(frame_spec, UncheckedIPv4Tuple):
                fields["ipv4_destination"] = frame_spec.destination_ip_address
                if frame_spec.source_ip_address is not None and frame_spec.source_ip_address != IPv4Tuple.ANY_SRC_IP:
                    fields["ipv4_source"] = frame_spec.source_ip_address
            else:
                fields["ipv6_destination"] = frame_spec.destination_ip_address
                if frame_spec.source_ip_address is not None and frame_spec.source_ip_address != IPv6Tuple.ANY_SRC_IP:
                    fields["ipv6_source"] = frame_spec.source_ip_address

            if frame_spec.protocol is not None and frame_spec.protocol != IPv4Tuple.ANY_PROTOCOL:
                fields["ip_protocol"] = frame_spec.protocol
                if frame_spec.protocol == PROTOCOL_UDP:
                    if frame_spec.destination_port:
                        fields["udp_destination_port"] = frame_spec.destination_port
                    if frame_spec.source_port:
                        fields["udp_source_port"] = frame_spec.source_port
                elif frame_spec.protocol == PROTOCOL_TCP:
                    if frame_spec.destination_port:
                        fields["tcp_destination_port"] = frame_spec.destination_port
                    if frame_spec.source_port:
                        fields["tcp_source_port"] = frame_spec.source_port
                else:
                    raise Exception("invalid protocol: %d" % frame_spec.protocol)

            if frame_spec.dscp is not None and frame_spec.dscp != IPv4Tuple.NO_DSCP:
                fields["ip_dscp"] = frame_spec.dscp

            return BaseMatch(**fields)

        else:
            raise Exception("invalid frame_spec type: %s" % frame_spec.__class__)

class QccPartialStream(IRTPartialStream):

    __slots__ = ("_associated_listener",

                 )

    def __init__(self, receiver, parent, listener):
        super(QccPartialStream, self).__init__(receiver, parent)
        self._status_changed = False
        self._associated_listener = listener

    def update_status(self):
        """
        sends a status object update to the uni client if anything has changed
        :return:
        """
        if self._status_changed:
            status = Status(
                stream_id=self.parent.stream_id,
                status_info=StatusInfo(
                    talker_status=self.parent.talker_status,
                    listener_status=self.parent.listener_status,
                    failure_code=self._status_code
                ),
                accumulated_latency=self._latency,
                interface_configuration=None,  # fixme: implement
                failed_interfaces=None,  # fixme: implement
                associated_talkerlistener=self._associated_listener
            ).notify_uni_client()
            self._status_changed = False

    @property
    def associated_listener(self):
        return self._associated_listener

class QccMultiStream(IRTMultiStream):
    _partialstream_class = QccPartialStream

    __slots__ = (
        "_associated_talker",

        "_max_latency",
        "_talker_status",
        "_listener_status"
    )

    def __init__(self, talker, sender):
        #fixme: implement multiple interfaces per host. requires changes to odl client
        super(QccMultiStream, self).__init__(
            sender=sender,
            receivers=set(),
            flow_match=QccMatch.from_framespec(talker.data_frame_specification),
            transmission_schedule=None,  # fixme: dummy  - need a better model in IRTMultiStream...
            maximum_latency=0,  # fixme: dummy
            maximum_jitter=0,  # fixme: dummy
            name=talker.name if talker.name else talker.stream_id
        )
        self._associated_talker = talker
        self._talker_status = TalkerStatus.No
        self._listener_status = ListenerStatus.No

    @property
    def talker_status(self):
        return self._talker_status

    @property
    def listener_status(self):
        return self._listener_status

    def set_status(self, status_code):
        super(QccMultiStream, self).set_status(status_code)

        # calculate more status information from partial streams
        new_listener_status = ListenerStatus.No
        if len(self._partials) > 0:
            listenersFailed = False
            listenersReady = False
            for partialstream in self._partials:
                if partialstream.status_code == FailureCode.NoFailure:
                    listenersReady = True
                else:
                    listenersFailed = True
            if listenersFailed:
                if listenersReady:
                    new_listener_status = ListenerStatus.PartialFailed
                else:
                    new_listener_status = ListenerStatus.Failed
            else:
                assert listenersReady  # since we checked if there is at least a partialstream, we can assume listenersReady.
                new_listener_status = ListenerStatus.Ready
        if new_listener_status != self._listener_status:
            self._status_changed = True
            self._listener_status = new_listener_status

        new_talker_status = TalkerStatus.Ready if status_code==FailureCode.NoFailure else TalkerStatus.Failed
        if new_listener_status != self._talker_status:
            self._status_changed = True
            self._talker_status = new_talker_status


    def update_status(self):
        """
        sends a status object update to the uni client if anything has changed
        :return:
        """
        if self._status_changed:
            Status(
                stream_id=self.stream_id,
                status_info=StatusInfo(
                    talker_status=self._talker_status,
                    listener_status=self._listener_status,
                    failure_code=self._status_code
                ),
                accumulated_latency=self._max_latency,
                interface_configuration=None,  # fixme: implement
                failed_interfaces=None,  # fixme: implement
                associated_talkerlistener=self._associated_talker
            ).notify_uni_client()
            self._status_changed = False

    @property
    def stream_id(self):
        return self._associated_talker.stream_id

    def add_listener(self, listener, receiver):
        """

        :param ODLClient odl_client:
        :param listener:
        :return:
        """
        #fixme: implement multiple interfaces per host. requires changes to odl client
        return self.add_receiver(receiver, listener=listener)


class QccStreamManager(object):
    """
    This object manages the relationship between 802.1Qcc talker/listener objects,
    And PartialStream/Multistream objects for odl_client.

    There is an equivalence between Talker and Multistream, and Listener and Partialstream.

    Every Talker has a MultiStream object associated with it. It may have 0 PartialStreams.

    Every Listener with a matching Talker (i.e., a talker with same StreamID) has a PartialStream
    associated with it.

    A Listener without a matching Talker cannot be modelled in terms of odl_client streams,
    and is placed in a waiting list.
    """

    __slots__ = (
        "_talker_associations",
        "_listener_associations",
        "_listeners_waiting",
        "_odl_client",
        "_lock"
    )

    def __init__(self, odl_client):
        self._lock = RLock()
        self._odl_client = odl_client
        self._talker_associations = {}  # type: dict[str, tuple[Talker, QccMultiStream]]
        self._listener_associations = {}  # type: dict[str, set[tuple[Listener, QccPartialStream]]]
        self._listeners_waiting = {}  # type: dict[str, set[Listener]]

    def add_talker(self, talker, sender):
        """

        :param Talker talker:
        :param CapacityBasedHost sender:
        :return:
        """
        # fixme: check duplicates
        with self._lock:
            stream_id = str(talker.stream_id)
            multistream = QccMultiStream(talker, sender)
            self._talker_associations[stream_id] = (talker, multistream)

            if self._listeners_waiting.get(stream_id, None):
                for l in self._listeners_waiting[stream_id]:
                    self.add_listener(l)
                del self._listeners_waiting[stream_id]
            # fixme: we need to return a status object iff we add the talker here without a listener. However, we may add a listener in a second or so...
            # return Status(
            #     stream_id = listener.stream_id,
            #     status_info=StatusInfo(
            #         talker_status=TalkerStatus.No,
            #         listener_status=ListenerStatus.Failed,
            #         failure_code=FailureCode.NoFailure
            #     ),
            #     accumulated_latency=0,
            #     interface_configuration=None,  # fixme: implement
            #     failed_interfaces=None,  # fixme: implement
            #     associated_talkerlistener=listener
            # )

    def add_listener(self, listener, receiver):
        """

        :param Listener listener:
        :param CapacityBasedHost sender:
        :return:
        """
        # fixme: check duplicates
        with self._lock:
            stream_id = str(listener.stream_id)
            if stream_id in self._talker_associations:
                # a talker for the stream is known.
                partialstream = self._talker_associations[stream_id][1].add_listener(listener, receiver)
                if stream_id in self._listener_associations:
                    self._listener_associations[stream_id].add((listener, partialstream))
                else:
                    self._listener_associations[stream_id] = {(listener, partialstream)}
            else:
                # no talker is known!
                if stream_id in self._listeners_waiting:
                    self._listeners_waiting[stream_id].add(listener)
                else:
                    self._listeners_waiting[stream_id] = {listener}
                # fixme: we need to return a status object iff we add the listener here without a talker. However, we may add a talker in a second or so...
                # return Status(
                #     stream_id = listener.stream_id,
                #     status_info=StatusInfo(
                #         talker_status=TalkerStatus.No,
                #         listener_status=ListenerStatus.Failed,
                #         failure_code=FailureCode.NoFailure
                #     ),
                #     accumulated_latency=0,
                #     interface_configuration=None,  # fixme: implement
                #     failed_interfaces=None,  # fixme: implement
                #     associated_talkerlistener=listener
                # )

    def remove_talker(self, talker):
        """

        :param Talker talker:
        :return:
        """
        # fixme: implement
        raise NotImplementedError()

    def remove_listener(self, listener):
        """

        :param Listener listener:
        :return:
        """
        # fixme: implement
        raise NotImplementedError()

    def get_partialstreams(self):
        """
        Get all PartialStream objects. This is the set of partialstreams that needs to be scheduled.
        :rtype: set[MultiStream]
        :return:
        """
        result = set()
        for l in self._listener_associations.values():
            result.update(partialstream for listener, partialstream in l)
        return result

    def check_for_status_updates(self):
        with self._lock:
            for _, multistream in self._talker_associations.values():
                multistream.update_status()
                for partialstream in multistream.partials:
                    partialstream.update_status()
