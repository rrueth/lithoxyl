# -*- coding: utf-8 -*-

import os
import sys
import atexit
import signal

from lithoxyl.actors import IntervalThreadActor

DEFAULT_JOIN_TIMEOUT = 0.5

LITHOXYL_CONTEXT = None


def get_context():
    if not LITHOXYL_CONTEXT:
        set_context(LithoxylContext())

    return LITHOXYL_CONTEXT


def set_context(context):
    global LITHOXYL_CONTEXT

    LITHOXYL_CONTEXT = context

    return context


def note(name, message, *a, **kw):
    return get_context().note(name, message, *a, **kw)


class LithoxylContext(object):
    def __init__(self, **kwargs):
        self.loggers = []

        self.async_mode = False
        self.async_actor = None
        self.async_timeout = DEFAULT_JOIN_TIMEOUT
        self._async_atexit_registered = False

        self.note_handlers = []

    def note(self, name, message, *a, **kw):
        """Lithoxyl can't use itself internally. This is a hook for recording
        all of those error conditions that need to be robustly
        ignored, such as if a user's message doesn't format correctly
        at runtime.
        """
        if not self.note_handlers:
            return
        if a:
            try:
                message = message % a
            except Exception:
                pass
        for nh in self.note_handlers:
            nh(name, message)
        return

    def _sync_parent_getter(self, record):
        import weakref

        logger = record.logger

        try:
            rec_tree = self._record_tree[logger]
        except AttributeError:
            self._record_tree = {}
            rec_tree = self._record_tree[logger] = weakref.WeakKeyDictionary()
            self._last_logged = weakref.WeakKeyDictionary({logger: None})
        except KeyError:
            rec_tree = self._record_tree[logger] = weakref.WeakKeyDictionary()

        try:
            ret = rec_tree[record]
        except KeyError:
            # haven't seen this one before
            if self._last_logged.get(logger):
                ret = self._last_logged[logger]
                # TODO: might be belt and braces
                if ret.record_id < record.record_id:
                    self._last_logged[logger] = record
            else:
                # nothing logged yet
                ret = None
                self._last_logged[logger] = record

            rec_tree[record] = ret
        return ret

    def enable_async(self, **kwargs):
        update_loggers = kwargs.pop('update_loggers', True)
        update_sigterm = kwargs.pop('update_sigterm', True)
        update_actor = kwargs.pop('update_actor', True)
        actor_kw = {'task': self.flush,
                    'interval': kwargs.pop('interval', None),
                    'max_interval': kwargs.pop('max_interval', None),
                    # be very careful when not daemonizing thread
                    'daemonize_thread': kwargs.pop('daemonize_thread', True),
                    'note': self.note}
        if kwargs:
            raise TypeError('unexpected keyword arguments: %r' % kwargs.keys())

        self.async_mode = True

        if update_sigterm:
            install_sigterm_handler()

        if update_actor:
            if not self.async_actor:
                self.async_actor = IntervalThreadActor(**actor_kw)
            self.async_actor.start()

        if update_loggers:
            for logger in self.loggers:
                logger.set_async(False)

        # graceful thread shutdown and sink flushing
        if not self._async_atexit_registered:
            # disable_async is safe to call multiple times but this is cleaner
            atexit.register(self.disable_async)
            self._async_atexit_registered = True

        return

    def disable_async(self, **kwargs):
        update_loggers = kwargs.pop('update_loggers', True)
        update_sigterm = kwargs.pop('update_sigterm', True)
        update_actor = kwargs.pop('update_actor', True)
        join_timeout = kwargs.pop('join_timeout', self.async_timeout)

        if update_sigterm:
            uninstall_sigterm_handler()

        if update_actor and self.async_actor:
            self.async_actor.stop()
            self.async_actor.join(join_timeout)

        if update_loggers:
            for logger in self.loggers:
                logger.set_async(False)

        self.flush()
        self.async_mode = False

    def flush(self):
        for logger in self.loggers:
            try:
                logger.flush()
            except Exception as e:
                self.note('context_flush',
                          'exception during logger (%r) flush: %r', logger, e)
        return

    def add_logger(self, logger):
        if logger not in self.loggers:
            self.loggers.append(logger)

    def remove_logger(self, logger):
        try:
            self.loggers.remove(logger)
        except ValueError:
            pass


class SyncTreeTracker(object):
    def __init__(self):
        self.tree = {}

    def get_parent(self, logger, record):
        try:
            stack = self.tree[logger]
        except KeyError:
            self.tree[logger] = []
        try:
            ret = stack[-1]
        except IndexError:
            ret = None
        stack.append(self)
        return ret


def signal_sysexit(signum, frame):
    # return code ends up being 143 for sigterm, See page 544 Kerrisk
    # for more see atexit_reissue_sigterm docstring for more details
    atexit.register(atexit_reissue_sigterm)
    sys.exit(143)  # approximate sigterm (128 + 15)


def atexit_reissue_sigterm():
    """The only way to "transparently" handle SIGTERM and terminate with
    the same status code as if we did not have a handler installed is
    to uninstall the handler and reissue the SIGTERM signal. Kerrisk
    p549 details more.

    So our signal handler registers this atexit handler, which calls
    :func:`os.kill`.

    Because that will end the process, extra precautions are built in
    to make sure we are the last exit handler running.

    """
    # best attempt at ensuring that we run last
    global _ASYNC_ATEXIT_ATTEMPT_LAST
    try:
        func, _, _ = atexit._exithandlers[0]
        if func is not atexit_reissue_sigterm and _ASYNC_ATEXIT_ATTEMPT_LAST:
            _ASYNC_ATEXIT_ATTEMPT_LAST = False
            atexit._exithandlers.insert(0, (atexit_reissue_sigterm, (), {}))
            return
    except IndexError:
        pass  # _exithandlers is empty, this is the last exitfunc
    except Exception:
        # TODO: effing atexit runs exitfuncs LIFO. If we os.exit early,
        # it's less grace, so abort reissuing the signal
        return

    uninstall_sigterm_handler(force=True)
    os.kill(os.getpid(), 15)
    return


_ASYNC_ATEXIT_ATTEMPT_LAST = True


def install_sigterm_handler():
    """This installs a no-op Python SIGTERM handler to ensure that atexit
    functions are called. If there is already a SIGTERM handler, no
    new handler is installed.
    """
    cur = signal.getsignal(signal.SIGTERM)
    if cur == signal.SIG_DFL:
        signal.signal(signal.SIGTERM, signal_sysexit)
        return True
    return False


def uninstall_sigterm_handler(force=False):
    cur = signal.getsignal(signal.SIGTERM)
    if force or cur is signal_sysexit:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return True
    return False
