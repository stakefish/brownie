#!/usr/bin/python3

import json
import time
import warnings
from collections import OrderedDict
from pathlib import Path
from threading import Lock, Thread
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union, ValuesView

import eth_event
from eth_event import EventError
from web3._utils import filters
from web3.datastructures import AttributeDict

from brownie._config import _get_data_folder
from brownie.convert.datatypes import ReturnValue
from brownie.convert.normalize import format_event
from brownie.exceptions import EventLookupError

from .web3 import ContractEvent, web3


class EventDict:
    """
    Dict/list hybrid container, base class for all events fired in a transaction.
    """

    def __init__(self, events: Optional[List] = None) -> None:
        """Instantiates the class.

        Args:
            events: event data as supplied by eth_event.decode_logs or eth_event.decode_trace"""
        if events is None:
            events = []

        self._ordered = [
            _EventItem(
                i["name"],
                i["address"],
                [OrderedDict((x["name"], x["value"]) for x in i["data"])],
                (pos,),
            )
            for pos, i in enumerate(events)
        ]

        self._dict: Dict = OrderedDict()
        for event in self._ordered:
            if event.name not in self._dict:
                events = [i for i in self._ordered if i.name == event.name]
                self._dict[event.name] = _EventItem(
                    event.name, None, events, tuple(i.pos[0] for i in events)
                )

    def __repr__(self) -> str:
        return str(self)

    def __bool__(self) -> bool:
        return bool(self._ordered)

    def __contains__(self, name: str) -> bool:
        """returns True if an event fired with the given name."""
        return name in [i.name for i in self._ordered]

    def __getitem__(self, key: Union[str, int]) -> "_EventItem":
        """if key is int: returns the n'th event that was fired
        if key is str: returns a _EventItem dict of all events where name == key"""
        if not isinstance(key, (int, str)):
            raise TypeError(f"Invalid key type '{type(key)}' - can only use strings or integers")
        if isinstance(key, int):
            try:
                return self._ordered[key]
            except IndexError:
                raise EventLookupError(
                    f"Index out of range - only {len(self._ordered)} events fired"
                )
        if key in self._dict:
            return self._dict[key]
        raise EventLookupError(f"Event '{key}' did not fire.")

    def __iter__(self) -> Iterator:
        return iter(self._ordered)

    def __len__(self) -> int:
        """returns the number of events that fired."""
        return len(self._ordered)

    def __str__(self) -> str:
        return str(dict((k, [i[0] for i in v._ordered]) for k, v in self._dict.items()))

    def count(self, name: str) -> int:
        """EventDict.count(name) -> integer -- return number of occurrences of name"""
        return len([i.name for i in self._ordered if i.name == name])

    def items(self) -> List:
        """EventDict.items() -> a list object providing a view on EventDict's items"""
        return list(self._dict.items())

    def keys(self) -> List:
        """EventDict.keys() -> a list object providing a view on EventDict's keys"""
        return list(self._dict.keys())

    def values(self) -> ValuesView:
        """EventDict.values() -> a list object providing a view on EventDict's values"""
        return self._dict.values()


class _EventItem:
    """
    Dict/list hybrid container, represents one or more events with the same name
    that were fired in a transaction.

    Attributes
    ----------
    name : str
        Name of the event.
    address : str
        Address where this event fired. When the object represents more than one event,
        this value is set to `None`.
    pos : tuple
        Tuple of indexes where this event fired.
    """

    def __init__(self, name: str, address: Optional[str], event_data: List, pos: Tuple) -> None:
        self.name = name
        self.address = address
        self._ordered = event_data
        self.pos = pos

    def __getitem__(self, key: Union[int, str]) -> List:
        """if key is int: returns the n'th event that was fired with this name
        if key is str: returns the value of data field 'key' from the 1st event
        within the container"""
        if not isinstance(key, (int, str)):
            raise TypeError(f"Invalid key type '{type(key)}' - can only use strings or integers")
        if isinstance(key, int):
            try:
                return self._ordered[key]
            except IndexError:
                raise EventLookupError(
                    f"Index out of range - only {len(self._ordered)} '{self.name}' events fired"
                )
        if key in self._ordered[0]:
            return self._ordered[0][key]
        if f"{key} (indexed)" in self._ordered[0]:
            return self._ordered[0][f"{key} (indexed)"]
        valid_keys = ", ".join(self.keys())
        raise EventLookupError(
            f"Unknown key '{key}' - the '{self.name}' event includes these keys: {valid_keys}"
        )

    def __contains__(self, name: str) -> bool:
        """returns True if this event contains a value with the given name."""
        return name in self._ordered[0]

    def __len__(self) -> int:
        """returns the number of events held in this container."""
        return len(self._ordered)

    def __repr__(self) -> str:
        return str(self)

    def __str__(self) -> str:
        if len(self._ordered) == 1:
            return str(self._ordered[0])
        return str([i[0] for i in self._ordered])

    def __iter__(self) -> Iterator:
        return iter(self._ordered)

    def __eq__(self, other: object) -> bool:
        if len(self._ordered) == 1:
            if isinstance(other, (tuple, list, ReturnValue)):
                # sequences compare directly against the event values
                return self._ordered[0].values() == other
            return other == self._ordered[0]
        return other == self._ordered

    def items(self) -> ReturnValue:
        """_EventItem.items() -> a list object providing a view on _EventItem[0]'s items"""
        return ReturnValue([(i, self[i]) for i in self.keys()])

    def keys(self) -> ReturnValue:
        """_EventItem.keys() -> a list object providing a view on _EventItem[0]'s keys"""
        return ReturnValue([i.replace(" (indexed)", "") for i in self._ordered[0].keys()])

    def values(self) -> ReturnValue:
        """_EventItem.values() -> a list object providing a view on _EventItem[0]'s values"""
        return ReturnValue(self._ordered[0].values())


class EventWatchData:
    def __init__(
        self,
        event: ContractEvent,
        callback: Callable[[AttributeDict], None],
        delay: float = 2.0,
        repeat: bool = True,
        from_block: int = None,
    ) -> None:
        # Args
        self.event = event
        self.callback = callback
        self.delay = delay
        self.repeat = repeat
        # Members
        self._event_filter: filters.LogFilter = event.createFilter(
            fromBlock=(from_block if from_block is not None else web3.eth.block_number - 1)
        )
        self._cooldown_time_over: bool = False
        self.timer = time.time()

    def get_new_events(self) -> List["filters.LogReceipt"]:
        return self._event_filter.get_new_entries()

    def reset_timer(self) -> None:
        self.timer = time.time()

    def _trigger_callback(self, events_data: List[AttributeDict]) -> None:
        self.cooldown_time_over = False
        for data in events_data:
            self.callback(data)

    @property
    def time_left(self) -> float:
        """Computes and returns the difference between the self.delay variable
        and the time between now and the last callback_trigger_time.

        Returns:
            float: Time difference between self.delay and the time between
            now and the last callback_trigger_time.
        """
        return max(float(0), self.delay - (time.time() - self.timer))


class EventWatcher:
    """
    Class containing methods to set callbacks on some specific events.
    This class is multi-threaded :
        - The main thread activates the two sub-threads and can be used
        to add callback instructions on a specific event.
        - The first sub-thread looks for new events among the ones with
        a callback set. When found, adds an order to execute the callback
        with the event data in a queue.
        - The second sub-thread executes the callbacks in the queue.
    """

    def __init__(self) -> None:
        self.target_list_lock: Lock = Lock()
        self.target_events_watch_data: List[EventWatchData] = []
        # self._queue: queue.Queue = queue.Queue()
        self._kill: bool = False
        self._kill_callbacks: bool = False
        self._has_started: bool = False
        self._watcher_thread = Thread(target=self._watch_loop, daemon=True)
        # self._callback_thread = Thread(target=self._execute_callbacks, daemon=True)

    def __del__(self) -> None:
        self.stop()

    def stop(self, wait: bool = True) -> None:
        """Stops the running thread within the instance.
        This function does not reset the member variables.

        Args:
            wait (bool, optional): Wether to wait for thread to join within the function.
                Defaults to True.
        """
        # Kill event catcher thread
        self._kill = True
        if wait is True and self._watcher_thread.is_alive():
            self._watcher_thread.join()
        # Kill callback executer thread.
        # self._kill_callbacks = True
        # if wait is True and self._callback_thread.is_alive():
        #     self._callback_thread.join()
        self._has_started = False

    def reset(self) -> None:
        """Stops the running threads and reset the instance to its basic state"""
        self.stop()
        self._setup()

    def add_event_callback(
        self,
        event: ContractEvent,
        callback: Callable[[AttributeDict], None],
        delay: float = 2.0,
        repeat: bool = True,
        from_block: int = None,
    ) -> None:
        """Adds a callback instruction for the specified event.

        Args:
            event (ContractEvent): The ContractEvent instance to watch for.
            callback (Callable[[AttributeDict], None]): The function to be called
                when a new 'event' is detected.
            delay (float, optional): The delay between each check for new 'event'(s).
                Defaults to 2.0.
            repeat (bool, optional): Wether to repeat the callback or not (if False,
                the callback will be called once only). Defaults to True.
            from_block (int, optional): The first block in which to look for 'event'(s).
                Defaults to None.

        Raises:
            TypeError: Raises when the parameter 'callback' is not a callable object.
        """
        if self._has_started is False:
            self._start_threads()
        if not callable(callback):
            raise TypeError("Argument 'callback' argument must be a callable.")
        delay = max(delay, 0.05)
        self.target_list_lock.acquire()  # lock
        self.target_events_watch_data.append(
            EventWatchData(event, callback, delay, repeat, from_block)
        )
        self.target_list_lock.release()  # unlock

    def _setup(self) -> None:
        """Sets up the EventWatcher instance member variables so it is ready to run"""

        self.target_list_lock.acquire()
        self.target_events_watch_data.clear()
        self.target_list_lock.release()
        # self._queue = queue.Queue()
        self._kill = False
        self._kill_callbacks = False
        self._has_started = False
        self._watcher_thread = Thread(target=self._watch_loop, daemon=True)
        # self._callback_thread = Thread(target=self._execute_callbacks, daemon=True)

    def _start_threads(self) -> None:
        """Starts two new Thread running the _watch_loop and the _execute_callbacks method."""
        self._watcher_thread.start()
        # self._callback_thread.start()
        self._has_started = True

    # def _execute_callbacks(self) -> None:
    #     """
    #     Executes the callbacks instructions stored in 'self._queue'
    #     """
    #     # Open ThreadPool with 4 workers to execute callbacks
    #     thread_pool = ThreadPool(processes=4)

    #     while not self._kill_callbacks:
    #         try:
    #             while self._queue.qsize() > 0:
    #                 # @dev: Not using Queue.get method for cross-platform reasons.
    #                 #   @see: https://docs.python.org/3/library/queue.html#queue.Queue.get
    #                 # Raises queue.Empty exception if queue is empty
    #                 task_data = self._queue.get_nowait()
    #                 # Execute callbacks with new events data
    #                 thread_pool.apply_async(
    #                     func=task_data["function"], args=(task_data["events_data"],)
    #                 )
    #         except queue.Empty:
    #             pass
    #         # Sleep a few before checking for new events
    #         # (avoids looping at max computer speed when self._queue is empty)
    #         time.sleep(0.05)
    #     # Force close and join threads within ThreadPool
    #     thread_pool.terminate()
    #     thread_pool.join()

    def _watch_loop(self) -> None:
        """
        Watches for new events, whenever new events are detected, stores the instruction
        to use callback on the detected events data in self._queue.
        """
        workers_list: List[Thread] = []

        while not self._kill:
            try:
                sleep_time: float = 1.0  # Max sleep time.
                self.target_list_lock.acquire()  # lock
                for elem in self.target_events_watch_data:
                    # If cooldown is not over :
                    #   skip and store time left before next check if needed.
                    time_left = elem.time_left
                    if time_left > 0:
                        sleep_time = min(sleep_time, time_left)
                        continue
                    # Check for new events & execute callback async if some are found
                    latest_events = elem.get_new_events()
                    if len(latest_events) != 0:
                        workers_list.append(
                            Thread(
                                target=elem._trigger_callback,
                                name="Callback Executor",
                                args=(latest_events,),
                            )
                        )
                        workers_list[-1].start()
                    elem.reset_timer()
                    # after elem.reset_timer elem.time_left is approximately elem.delay
                    sleep_time = min(sleep_time, elem.time_left)
            finally:
                # Remove not repeating subscriptions
                self.target_events_watch_data = list(
                    filter(lambda x: x.repeat, self.target_events_watch_data)
                )
                workers_list = list(filter(lambda x: x.is_alive(), workers_list))
                self.target_list_lock.release()  # unlock
                time.sleep(sleep_time)
        # Join running threads
        for worker_instance in workers_list:
            worker_instance.join()


def __get_path() -> Path:
    return _get_data_folder().joinpath("topics.json")


def _get_topics(abi: List) -> Dict:
    topic_map = eth_event.get_topic_map(abi)

    updated_topics = _topics.copy()

    for key, value in topic_map.items():
        if key not in updated_topics:
            # new event topic
            updated_topics[key] = value
        elif value == updated_topics[key]:
            # existing event topic, nothing has changed
            continue
        elif not next((i for i in updated_topics[key]["inputs"] if i["indexed"]), False):
            # existing topic, but the old abi has no indexed events - keep the new one
            updated_topics[key] = value

    if updated_topics != _topics:
        _topics.update(updated_topics)
        with __get_path().open("w") as fp:
            json.dump(updated_topics, fp, sort_keys=True, indent=2)

    return {v["name"]: k for k, v in topic_map.items()}


def _add_deployment_topics(address: str, abi: List) -> None:
    _deployment_topics[address] = eth_event.get_topic_map(abi)


def _decode_logs(logs: List, contracts: Optional[Dict] = None) -> EventDict:
    if not logs:
        return EventDict()

    idx = 0
    events: List = []
    while True:
        address = logs[idx]["address"]
        try:
            new_idx = logs.index(next(i for i in logs[idx:] if i["address"] != address))
            log_slice = logs[idx:new_idx]
            idx = new_idx
        except StopIteration:
            log_slice = logs[idx:]

        topics_map = _deployment_topics.get(address, _topics)
        for item in log_slice:
            if contracts and contracts[item.address]:
                note = _decode_ds_note(item, contracts[item.address])
                if note:
                    events.append(note)
                    continue
            try:
                events.extend(eth_event.decode_logs([item], topics_map, allow_undecoded=True))
            except EventError as exc:
                warnings.warn(f"{address}: {exc}")

        if log_slice[-1] == logs[-1]:
            break

    events = [format_event(i) for i in events]
    return EventDict(events)


def _decode_ds_note(log, contract):  # type: ignore
    # ds-note encodes function selector as the first topic
    selector, tail = log.topics[0][:4], log.topics[0][4:]
    if selector.hex() not in contract.selectors or sum(tail):
        return
    name = contract.selectors[selector.hex()]
    data = bytes.fromhex(log.data[2:])
    # data uses ABI encoding of [uint256, bytes] or [bytes] in different versions
    # instead of trying them all, assume the payload starts from selector
    try:
        func, args = contract.decode_input(data[data.index(selector) :])
    except ValueError:
        return
    return {
        "name": name,
        "address": log.address,
        "decoded": True,
        "data": [
            {"name": abi["name"], "type": abi["type"], "value": arg, "decoded": True}
            for arg, abi in zip(args, contract.get_method_object(selector.hex()).abi["inputs"])
        ],
    }


def _decode_trace(trace: Sequence, initial_address: str) -> EventDict:
    if not trace:
        return EventDict()

    events = eth_event.decode_traceTransaction(
        trace, _topics, allow_undecoded=True, initial_address=initial_address
    )
    events = [format_event(i) for i in events]
    return EventDict(events)


# dictionary of event topic ABIs specific to a single contract deployment
_deployment_topics: Dict = {}

# general event topic ABIs for decoding events on unknown contracts
_topics: Dict = {}

# EventWatcher program instance
event_watcher = EventWatcher()

try:
    with __get_path().open() as fp:
        _topics = json.load(fp)
except (FileNotFoundError, json.decoder.JSONDecodeError):
    pass
