# Copyright (c) 2021 Incisive Technology Ltd
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
The watch module provides support for performing K8s watch operations on resources

The objects in this module rely on metadata in the various Hikaru models to
determine to to initiate watches on underlying Kubernetes resources using the APIs
provided by Kubernetes. The intent is to yield an easier-to-use watch mechanism that
be utilized in a number of different operational processes.

There are three main classes in this module:

- Watcher, which provides a mechanism to receive a stream of resource events from
  K8s as they occur,
- MultiplexingWatcher, which provides a way to merge the streams of multiple Watcher
  objects into a single stream of events,
- WatchEvent, which is the container for the streamed events and contains a event type
  code and a Hikaru object which is the event that has been received.

General use is simple:

- Create the object with the necessary initialization parameters,
- For the MultiplexingWatcher, add the Watchers to be managed,
- Invoke the stream() method to get a generator which will yield WatchEvent objects,
- Invoke stop() to cease delivery of WatchEvents and terminate the generator.

Either type of Watcher may be restarted after being stopped, and both provide a means
to indicate where in the event stream delivery of events should occur.

For further details, see the doc for each class.
"""

import importlib
import re
import threading
from typing import cast, Generator, Union
import queue

from kubernetes.client import ApiClient, ApiException
from kubernetes.watch import Watch
from hikaru import HikaruDocumentBase, from_dict
from hikaru.meta import WatcherDescriptor

_api_class_cache = {}


def _get_api_class(wd: WatcherDescriptor) -> type:
    key = (wd.pkgname, wd.modname, wd.clsname)
    cls = _api_class_cache.get(key)
    if cls is None:
        mod = importlib.import_module(wd.modname, wd.pkgname)
        cls = getattr(mod, wd.clsname)
        _api_class_cache[key] = cls
    return cls


def _k8s_watch_factory():
    return Watch()


_should_translate = True


class WatchEvent(object):
    """
    Delivery wrapper for an event on a resource received from K8s

    Instances of this class are what are yielded from the stream() generator
    on both Watcher and MultiplexingWatcher. Instances are internally generated by
    Hikaru's watch system.

    'etype' is the type of event, and will be one of 'ADDED', 'MODIFIED' or 'DELETED'

    'obj' is an instance of a HikaruDocumentBase subclass which is instantiated from
        event data received from K8s. Besides testing the type of this object, you can
        also look to obj.kind for a text representation of the 'kind' of object the
        event involves.

    In the case of etype MODIFIED, you can use the diff() method to compare the event's
    version with a previous version, revealing what has changed.
    """
    __slots__ = ['etype', 'obj']

    def __init__(self, etype: str, obj: HikaruDocumentBase):
        self.etype = etype
        self.obj = obj


class BaseWatcher(object):
    def __init__(self):
        self._run = False

    def stream(self, manage_resource_version=False,
               quit_on_timeout=False) -> Generator[WatchEvent, None, None]:
        yield None  # pragma: no cover

    def stop(self):
        self._run = False

    def start(self):
        self._run = True

    def isrunning(self) -> bool:
        return self._run


class Watcher(BaseWatcher):
    """
    Provides the basic machinery to watch for events on a K8s resource (kind)

    This is the core class used to create objects that can stream events on
    a single type of K8s resource. It ultimately provides a generator that yields
    a series of WatchEvent objects for each event received from Kubernetes.
    """
    _starting_resource_version = -1
    _rvre = re.compile(r'\(([0-9]+)\)')

    def __init__(self, cls: type,
                 namespace: str = None,
                 allow_watch_bookmarks: bool = None,
                 field_selector: str = None,
                 label_selector: str = None,
                 resource_version: Union[str, int] = None,
                 timeout_seconds: Union[int, None] = 1,
                 client: ApiClient = None):
        r"""
        Create a watch object for the specified class.

        Defines the configuration to use for setting up a watch against a particular
        kind of K8s resource. After configuration, watching can be initiated by calling
        the 'stream()' method.

        NOTE:

        The arguments here are largely interpreted according to the 'list' operations
        in the K8s API, with the exception of some additional semantics added by
        Hikaru; these are noted where appropriate. Not all arguments from those functions
        are carried through here as some make no sense in the context of a watch.

        :param cls: A watchable HikaruDocumentBase subclass (NOT instance). This may be
            either the class that will be watched and outputted, such as Pod,
            or the 'listing' class for this kind of item, such as PodList. In either
            case, the listing resource will NOT be returned from stream() (hence,
            no PodList will ever be in an event), only the listed resource (like Pod).
        :param namespace: optional string; namespace for the resource. If not specified,
            then all resources across all namespaces will be watched and returned
            from the stream() method. If specified, only resources from the specified
            namespace will be returned from stream(). NOTE: not all resources support
            namespaced watches; a TypeError is raised if there is no namespaced support
            for cls but a namespace value is provided.
        :param allow_watch_bookmarks: allowWatch Bookmarks requests watch events with
            type "BOOKMARK". Servers that do not implement bookmarks may ignore
            this flag and bookmarks are sent at the server's discretion. Clients
            should not assume bookmarks are returned at any specific interval,
            nor may they assume the server will send any BOOKMARK event during a
            session. If this is not a watch, this field is ignored. If the
            feature gate WatchBookmarks is not enabled in apiserver, this field
            is ignored.
        :param field_selector: A selector to restrict the list of returned objects by
            their fields. Defaults to everything.
        :param label_selector: A selector to restrict the list of returned objects by
            their labels. Defaults to everything.
        :param resource_version: When specified with a watch call, shows changes that
            occur after that particular version of a resource. Defaults to
            changes from the beginning of history. When specified for list: - if
            unset, then the result is returned from remote storage based on
            quorum-read flag; - if it's 0, then we simply return what we
            currently have in cache, no guarantee; - if set to non zero, then
            the result is at least as fresh as given rv.
            *Hikaru addition* you can allow Hikaru to find the oldest available
            resource_version when you initiate the stream(), but keep in mind that
            this can result in a fairly long stream of events that may have already been
            delivered. The the doc for the stream() method for more details.
        :param timeout_seconds: Timeout for the list/watch call. This limits the
            duration of the call, regardless of any activity or inactivity.
            *Hikaru additions* The stream() method of Watcher normally ignores a
            timeout of the underlying code and only stops streaming if the Watcher's
            stop() method has been invoked. However, Watchers default this value to 1
            second in order to allow the underlying code to return and give the Watcher
            a chance to see if it should quit. This allows for clean exits of the
            stream with a minimal delay. You may want to adjust this value upwards if
            you anticipate a large number of Watchers in your system. Setting this
            to zero will make the underlying call block indefinitely.
        :param client: optional; instance of kubernetes.client.api_client.ApiClient

        :raises TypeError: if the supplied cls parameter doesn't support the requested
            type of watch, namespaced or unnamespaced
        """
        if not issubclass(cls, HikaruDocumentBase):
            raise TypeError("cls must be a subclass of HikaruDocumentBase")

        if cls._watcher_cls is not None:
            watcher_cls = cls._watcher_cls
        else:
            watcher_cls = cls

        if namespace:
            if watcher_cls._namespaced_watcher is None:
                raise TypeError(f"{cls.__name__} has no namespaced watcher support")
            self.wd: WatcherDescriptor = watcher_cls._namespaced_watcher
        else:
            if watcher_cls._watcher is None:
                raise TypeError(f"{cls.__name__} has no watcher support")
            self.wd: WatcherDescriptor = watcher_cls._watcher

        super(Watcher, self).__init__()
        self.cls = cls
        self.kwargs = {'allow_watch_bookmarks': allow_watch_bookmarks,
                       'field_selector': field_selector,
                       'label_selector': label_selector,
                       'resource_version': (str(resource_version)
                                            if resource_version is not None
                                            else resource_version),
                       'timeout_seconds': timeout_seconds}
        if namespace is not None:
            self.kwargs['namespace'] = namespace
        self.client = client
        self.k8s_watcher = None
        apicls = _get_api_class(self.wd)
        inst = apicls(api_client=self.client)
        self.meth = getattr(inst, self.wd.methname)
        self.highest_resource_version = self._starting_resource_version

    def update_resource_version(self, new_resource_version: Union[str, int]):
        """
        Update the value of resource_version to use when initiating a stream

        This will only take effect the next time the ``stream()`` method is invoked.

        :param new_resource_version: int or numeric string containing a
            version number; only version after this version will be streamed.
        """
        if new_resource_version is None:
            raise RuntimeError("resource_version cannot be None")
        self.kwargs['resource_version'] = str(new_resource_version)

    def current_resource_version(self) -> str:
        """
        get the current resource version value for stream inintiation

        :return: string resource version, but can be None if this is a new
            Watcher, streaming hasn't begun, and the user didn't request that
            the Watcher manage the revision version number.
        """
        return self.kwargs['resource_version']

    def stop(self):
        """
        Stops the current Watcher and underlying watch mechanism

        Stopping will occur as soon as the Watcher itself regains control;
        if awaiting on the arrival of an event, the streaming operation won't
        stop until an event arrives.
        """
        if self.k8s_watcher is not None:
            self.k8s_watcher.stop()
        super(Watcher, self).stop()

    def stream(self, manage_resource_version: bool = False,
               quit_on_timeout: bool = False) -> Generator[WatchEvent, None, None]:
        """
        Initiate an event streaming generator that yields WatchEvent objects.

        Start yielding a stream of WatchEvents as events are received from K8s. You
        can use this in a for loop like so:

        .. code:: python

            for we in watcher.stream():
                # do stuff

        Iteration will only stop if:

        - There is an uncaught exception in the underlying K8s system
        - There was a timeout in the underlying K8s system and quit_on_timeout is True
        - You invoke the stop() method on the Watcher instance.

        :param manage_resource_version: defaults to False. If True, then the Watcher
            instance will track the highest resource_version observed in the event
            stream in case the underlying watch is to be used again. This is generally
            a good idea if you timeout value is 0 or None (no timeout), as it will ensure
            that subsequent internal calls to the underlying watch mechanism will only
            ask for events that come after the highest observed version number. It also
            will start up where you left off if the Watcher has been stopped and re-
            started. *NOTE* if you don't supply an initial resource_version value,
            then this option will also query K8s for the oldest available resource
            version and ask for the event stream to start after that point. This is
            handy as you don't need to track the resource_version yourself, but can
            result in a replay of a larger number of events that have previously been
            seen. You may want this, but you should be aware that this is possible.
        :param quit_on_timeout: defaults to False. If True, then when the underlying
            K8s code times out the generator stops. The default behaviour is for the
            generator to restart the underlying watch machinery.
        :return: A generator that will yield WatchEvent instances.
        """
        self.start()
        self.k8s_watcher = _k8s_watch_factory()
        if manage_resource_version and self.current_resource_version() is None:
            # do a priming run to determine the lowest resource_version available
            kwargs = dict(self.kwargs)
            kwargs['resource_version'] = '1'
            try:
                for _ in self.k8s_watcher.stream(self.meth, **kwargs):
                    raise RuntimeError("Unable to determine the oldest available "
                                       "resource version")  # pragma: no cover
            except ApiException as e:
                if e.status == 410 and e.reason.startswith('Expired:'):
                    m = self._rvre.search(e.reason)
                    if m is not None:
                        new_rv = m.group(1)
                        self.update_resource_version(new_rv)
                    else:
                        raise
                else:
                    raise

        while self.isrunning():
            if manage_resource_version:
                if self.current_resource_version() is not None:
                    if self.highest_resource_version > int(
                            self.current_resource_version()):
                        self.update_resource_version(self.highest_resource_version)
            try:
                for e in self.k8s_watcher.stream(self.meth, **self.kwargs):
                    if not self.isrunning():
                        break  # pragma: no cover
                    o: HikaruDocumentBase = cast(HikaruDocumentBase,
                                                 from_dict(e['object'].to_dict(),
                                                           translate=_should_translate))
                    new_rv = int(o.metadata.resourceVersion)
                    if new_rv > self.highest_resource_version:
                        self.highest_resource_version = new_rv
                    # THIS NEXT IF IS SOMEWHAT CONTROVERSIAL
                    # don't yield events with a lower resource version
                    # if new_rv < self.highest_resource_version:
                    #     continue
                    event = WatchEvent(e['type'], o)
                    if not self.isrunning():
                        break  # pragma: no cover
                    yield event
                    if not self.isrunning():
                        break
            except ApiException as e:
                if e.status == 410 and manage_resource_version:
                    m = self._rvre.search(e.reason)
                    if m is not None:
                        newrv = m.group(1)
                        self.update_resource_version(newrv)
                        continue
                raise
            if self.isrunning():
                # then we timed out; let's see if we should really quit
                if quit_on_timeout:
                    break


class MultiplexingWatcher(BaseWatcher):
    """
    A container of Watchers that yields a single stream of events from all Watchers

    This class provides an interface similar to Watcher, but allows you to weave
    together streams from multiple Watchers into a single event stream.
    """
    def __init__(self, exception_callback=None):
        """
        Create a new MultiplexingWatcher

        :param exception_callback: optional callable. If one of the contained watchers
            raises an exception and this callable has been provided, then the callable
            will be called as follows:

            .. code:: python

                exception_callback(mux, watcher, exception)

            Where `mux` is the MultiplexingWatcher instance, `watcher` is the Watcher
            that raised the exception, and `exception` is the Exception object that was
            raised. The callback can perform whatever action it wishes on mux or watcher
            based on the information in exception (generally an instance of
            kubernetes.client.ApiException). The return value from callback will determine
            how the MultiplexingWatcher will proceed with this watcher. If the value True
            is returned, the exception is ignored and the watching function is re-started.
            Any other value will silently cease processing events for this Watcher. If
            an exception is raised within the callback, this is treated as a non-True
            return value.
        """
        super(MultiplexingWatcher, self).__init__()
        self.watchers = {}
        self.results_queue = queue.Queue()
        self.manage_resource_version = None
        self.quit_on_timeout = None
        self.exception_callback = exception_callback

    def _start_watcher(self, watcher: Watcher):
        t = threading.Thread(target=self._run_watcher, args=(watcher,))
        t.start()

    def _run_watcher(self, watcher: Watcher):
        watcher_running = True
        while watcher_running and self.isrunning():
            try:
                for we in watcher.stream(manage_resource_version=self.manage_resource_version,
                                         quit_on_timeout=self.quit_on_timeout):
                    if watcher.isrunning() and self.isrunning():
                        self.results_queue.put(we)
                if self.quit_on_timeout:
                    watcher.stop()
            except Exception as e:
                if self.exception_callback is not None:
                    meth = self.exception_callback
                    try:
                        flag = meth(self, watcher, e)
                    except Exception as _:
                        flag = None
                else:
                    flag = None
                if flag is not True:
                    watcher.stop()
            watcher_running = watcher.isrunning()
        self.del_watcher(watcher)

    def add_watcher(self, watcher: Watcher):
        """
        Add a new Watcher to the set already managed.

        You can call this method at any time, even during streaming, however the old
        watcher may still emit some events before it knows to stop.

        NOTE: you can only have a single watcher for one type of resource; for example
        if you create two Watchers that specify Pod for the cls argument and then
        call add_watcher() with each in turn, only the second instance will be watched.

        :param watcher: A Watcher instance.
        """
        current_watcher: Watcher = self.watchers.get(watcher.cls)
        if current_watcher is not None:
            current_watcher.stop()
        self.watchers[watcher.cls] = watcher
        if self.isrunning():
            self._start_watcher(watcher)

    def del_watcher(self, watcher: Watcher):
        """
        Delete a watcher

        :param watcher: The watcher to delete.
        """
        watcher.stop()
        try:
            del self.watchers[watcher.cls]
        except KeyError:
            pass

    def stop(self):
        """
        Stop the multiplexor and all contained Watchers
        """
        super(MultiplexingWatcher, self).stop()
        for watcher in dict(self.watchers).values():
            watcher.stop()

    def stream(self, manage_resource_version: bool = False,
               quit_on_timeout: bool = False) -> Generator[WatchEvent, None, None]:
        """
        Initiate streaming of events from all contained Watchers

        Returns a generator that emits a stream of WatchEvent objects from all
        contained Watcher objects.

        You can use this in a for loop like so:

        .. code:: python

            mux = MultiplexingWatcher()
            mux.add_watcher(Watcher(Pod, timeout_seconds=1))
            mux.add_watcher(Watcher(Namespace, timeout_seconds=1))
            for we in mux.stream():
                # do stuff

        Generation will only stop if:

        - There was a timeout in the underlying K8s system and quit_on_timeout is True
        - You invoke the stop() method on the MultiplexingWatcher instance.

        :param manage_resource_version: defaults to False. Simply passed through to
            all contained Watcher instances. See the doc for Watcher.stream() for
            details on this argument.
        :param quit_on_timeout: defaults to False. Simply passed through to
            all contained Watcher instances. See the doc for Watcher.stream() for
            details on this argument.
        :return: A generator that will yield WatchEvent instances from all contained
            Watchers.
        """
        self.manage_resource_version = manage_resource_version
        self.quit_on_timeout = quit_on_timeout
        self.start()
        for watcher in dict(self.watchers).values():
            self._start_watcher(watcher)

        # now await messages to appear on the queue and yield them
        while self.isrunning() and self.watchers:
            try:
                event = self.results_queue.get(block=True, timeout=0.1)
                yield event
            except queue.Empty:
                pass


__all__ = ['Watcher', 'MultiplexingWatcher', 'WatchEvent']
