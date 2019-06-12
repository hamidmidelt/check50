import collections
from contextlib import contextmanager
import concurrent.futures as futures
import enum
import functools
import inspect
import importlib
import gettext
import os
from pathlib import Path
import shutil
import signal
import tempfile
import traceback

import attr

from . import internal
from .api import log, Failure, _copy, _log, _data

_check_names = []


@attr.s(slots=True)
class CheckResult:
    """Record returned by each check"""
    name = attr.ib()
    description = attr.ib()
    passed = attr.ib(default=None)
    log = attr.ib(default=attr.Factory(list))
    cause = attr.ib(default=None)
    data = attr.ib(default=attr.Factory(dict))
    dependency = attr.ib(default=None)

    @classmethod
    def from_check(cls, check, *args, **kwargs):
        """Create a check_result given a check function, automatically recording the name,
        the dependency, and the (translated) description.
        """
        return cls(name=check.__name__, description=_(check.__doc__),
                   dependency=check._check_dependency.__name__ if check._check_dependency else None,
                   *args,
                   **kwargs)


class Timeout(Failure):
    def __init__(self, seconds):
        super().__init__(rationale=_("check timed out after {} seconds").format(seconds))


@contextmanager
def _timeout(seconds):
    """Context manager that runs code block until timeout is reached.

    Example usage::

        try:
            with _timeout(10):
                do_stuff()
        except Timeout:
            print("do_stuff timed out")
    """

    def _handle_timeout(*args):
        raise Timeout(seconds)

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def check(dependency=None, timeout=60, hidden=False):
    """Mark function as a check.

    :param dependency: the check that this check depends on
    :type dependency: function
    :param timeout: maximum number of seconds the check can run
    :type timeout: int
    :param hidden: true if cause and log should be hidden from student
    :type hidden: bool

    When a check depends on another, the former will only run if the latter passes.
    Additionally, the dependent check will inherit the filesystem of its dependency.
    This is particularly useful when writing e.g., a ``compiles`` check that compiles a
    student's program (and checks that it compiled successfully). Any checks that run the
    student's program will logically depend on this check, and since they inherit the
    resulting filesystem of the check, they will immidiately have access to the compiled
    program without needing to recompile.

    Example usage::

        @check50.check() # Mark 'exists' as a check
        def exists():
            \"""hello.c exists\"""
            check50.exists("hello.c")

        @check50.check(exists) # Mark 'compiles' as a check that depends on 'exists'
        def compiles():
            \"""hello.c compiles\"""
            check50.c.compile("hello.c")

        @check50.check(compiles)
        def prints_hello():
            \"""prints "Hello, world!\\\\n\"""
            # Since 'prints_hello', depends on 'compiles' it inherits the compiled binary
            check50.run("./hello").stdout("[Hh]ello, world!?\\n", "hello, world\\n").exit()

    """
    def decorator(check):

        # Modules are evaluated from the top of the file down, so _check_names will
        # contain the names of the checks in the order in which they are declared
        _check_names.append(check.__name__)
        check._check_dependency = dependency

        @functools.wraps(check)
        def wrapper(checks_root, dependency_state):
            # Result template
            result = CheckResult.from_check(check)
            # Any shared (returned) state
            state = None

            try:
                # Setup check environment, copying disk state from dependency
                internal.run_dir = checks_root / check.__name__
                src_dir = checks_root / (dependency.__name__ if dependency else "-")
                shutil.copytree(src_dir, internal.run_dir)
                os.chdir(internal.run_dir)

                # Run registered functions before/after running check and set timeout
                with internal.register, _timeout(seconds=timeout):
                    args = (dependency_state,) if inspect.getfullargspec(check).args else ()
                    state = check(*args)
            except Failure as e:
                result.passed = False
                result.cause = e.payload if not hidden else {}
            except BaseException as e:
                result.passed = None
                result.cause = {"rationale": _("check50 ran into an error while running checks!")}
                log(repr(e))
                for line in traceback.format_tb(e.__traceback__):
                    log(line.rstrip())
                log(_("Contact sysadmins@cs50.harvard.edu with the URL of this check!"))
            else:
                result.passed = True
            finally:
                if not hidden:
                    result.log = _log
                result.data = _data
                return result, state
        return wrapper
    return decorator


# Probably shouldn't be a class
class CheckRunner:
    def __init__(self, checks_path):

        # TODO: Naming the module "checks" is arbitray. Better name?
        self.checks_spec = importlib.util.spec_from_file_location("checks", checks_path)

        # Clear check_names, import module, then save check_names. Not thread safe.
        # Ideally, there'd be a better way to extract declaration order than @check mutating global state,
        # but there are a lot of subtleties with using `inspect` or similar here
        _check_names.clear()
        check_module = importlib.util.module_from_spec(self.checks_spec)
        self.checks_spec.loader.exec_module(check_module)
        self.check_names = _check_names.copy()
        _check_names.clear()

        # Map each check to tuples containing the names and descriptions of the checks that depend on it
        self.dependency_map = collections.defaultdict(set)
        for name, check in inspect.getmembers(check_module, lambda f: hasattr(f, "_check_dependency")):
            dependency = check._check_dependency.__name__ if check._check_dependency is not None else None
            self.dependency_map[dependency].add((name, check.__doc__))


    def run(self, files, working_area):
        """
        Run checks concurrently.
        Returns a list of CheckResults ordered by declaration order of the checks in the imported module
        """

        # Ensure that dictionary is ordered by check declaration order (via self.check_names)
        # NOTE: Requires CPython 3.6. If we need to support older versions of Python, replace with OrderedDict.
        results = {name: None for name in self.check_names}
        checks_root = working_area.parent
        with futures.ProcessPoolExecutor() as executor:

            # Start all checks that have no dependencies
            not_done = set(executor.submit(run_check(name, self.checks_spec, checks_root))
                           for name, _ in self.dependency_map[None])
            not_passed = []

            while not_done:
                done, not_done = futures.wait(not_done, return_when=futures.FIRST_COMPLETED)
                for future in done:
                    # Get result from completed check
                    result, state = future.result()
                    results[result.name] = result
                    if result.passed:
                        # Dispatch dependent checks
                        for child_name, _ in self.dependency_map[result.name]:
                            not_done.add(executor.submit(
                                run_check(child_name, self.checks_spec, checks_root, state)))
                    else:
                        not_passed.append(result.name)

        for name in not_passed:
            self._skip_children(name, results)

        return list(results.values())


    def run_targetted(self, check_names, files, working_area):
        """
        Run just the targetted checks, and the checks they depends on.
        Returns just the result of the targetted checks.
        """
        if len(set(check_names)) < len(check_names):
            raise internal.Error(_("Duplicate checks targetted: {}".format(check_names)))

        # Reconstruct a new dependency_map, consisting of the targetted checks and their dependencies
        new_dependency_map = collections.defaultdict(set)

        # For every targetted check
        for check_name in check_names:

            # Get dependencies just for targetted check
            dependencies = self._get_dependencies(check_name)

            if not dependencies:
                raise internal.Error(_("Unknown check {}").format(check_name))

            # Create a dependency map just for check_name
            dependency_map = {a[0]:{b} for a,b in zip([(None, None)] + dependencies[:-1], dependencies)}

            # Merge dependency map with new_dependency_map
            for name, dependent in dependency_map.items():
                new_dependency_map[name] |= dependent

        # Temporarily replace dependency_map and run
        try:
            old_dependency_map = self.dependency_map
            self.dependency_map = new_dependency_map
            results = self.run(files, working_area)
        finally:
            self.dependency_map = old_dependency_map

        # Filter out all results except the targetted checks
        return [result for result in results if result and result.name in check_names]


    def _get_dependencies(self, check_name):
        """
        Gather all dependencies of a check.
        Returns a list of check_names in order of execution, or None if check_name does not exist.
        """
        # Find all checks with no dependencies
        checks = self.dependency_map[None]

        # If target check has no dependency return
        for other_check_name, description in checks:
            if check_name == other_check_name:
                return [(check_name, description)]

        # Depth-first search through the dependency tree
        # Keep track of all routes (lists of checks) on a stack
        routes = [[d] for d in checks]

        # While there are still unexplored routes
        while routes:
            # Visit most recent route
            cur_route = routes.pop()
            cur_end = cur_route[-1][0]

            # Gather all checks that follow the last check in route (connecting nodes)
            for dependency in self.dependency_map[cur_end]:
                # Create a new route for each check
                route = cur_route + [dependency]

                # If new route ends at the check we are looking for, return
                if dependency[0] == check_name:
                    return route

                # Otherwise, add route to the stack
                routes.append(route)


    def _skip_children(self, check_name, results):
        """
        Recursively skip the children of check_name (presumably because check_name
        did not pass).
        """
        for name, description in self.dependency_map[check_name]:
            if results[name] is None:
                results[name] = CheckResult(name=name, description=_(description),
                                            passed=None,
                                            dependency=check_name,
                                            cause={"rationale": _("can't check until a frown turns upside down")})
                self._skip_children(name, results)


class run_check:
    """
    Hack to get around the fact that `pickle` can't serialize closures.
    This class is essentially a function that reimports the check module and runs the check.
    """

    def __init__(self, check_name, spec, checks_root, state=None):
        self.check_name = check_name
        self.spec = spec
        self.checks_root = checks_root
        self.state = state

    def __call__(self):
        mod = importlib.util.module_from_spec(self.spec)
        self.spec.loader.exec_module(mod)
        internal.check_running = True
        try:
            return getattr(mod, self.check_name)(self.checks_root, self.state)
        finally:
            internal.check_running = False
