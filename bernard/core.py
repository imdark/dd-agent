import logging
import signal
import sys
import time
from daemon import Daemon
from util import (
    StaticWatchdog,
    get_os,
    yaml,
    yLoader,
)
from checks.check_status import AgentStatus, style
from config import get_config_path

from bernard.check import R, S
from bernard.scheduler import Scheduler

RESTART_INTERVAL = 4 * 24 * 60 * 60 # Defaults to 4 days
BERNARD_CONF = "bernard.yaml"

log = logging.getLogger(__name__)

class Bernard(Daemon):
    """
    The Bernard class is a daemon that runs the scheduler in a background process.
    """

    def __init__(self, pidfile, hostname, autorestart, start_event=True):
        """ Initialization of the Dameon """
        Daemon.__init__(self, pidfile)
        self.run_forever = True
        self.scheduler = None
        self.autorestart = autorestart
        self.start_event = start_event
        self.hostname = hostname
        StaticWatchdog.reset()

    def _handle_sigterm(self, signum, frame):
        log.debug("Caught sigterm. Stopping run loop.")
        self.run_forever = False

    def _handle_sigusr1(self, signum, frame):
        self._handle_sigterm(signum, frame)
        self._do_restart()

    def info(self, verbose=None):
        logging.getLogger().setLevel(logging.ERROR)
        return BernardStatus.print_latest_status(verbose=verbose)

    def run(self):
        """Main loop of Bernard"""

        # Gracefully exit on sigterm.
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        # A SIGUSR1 signals an exit with an autorestart
        signal.signal(signal.SIGUSR1, self._handle_sigusr1)

        # Handle Keyboard Interrupt
        signal.signal(signal.SIGINT, self._handle_sigterm)

        # load Bernard config and checks
        bernard_config = get_bernard_config()
        self.scheduler = Scheduler.from_config(self.hostname, bernard_config)

        # Save the agent start-up stats.
        BernardStatus(checks=self.scheduler.checks).persist()
        self.last_info_update = time.time()

        # Initialize the auto-restarter
        self.restart_interval = int(RESTART_INTERVAL)
        self.agent_start = time.time()

        # Run the main loop.
        while self.run_forever:
            # Run the next scheduled check
            self.scheduler.process()

            wait_time = self.scheduler.wait_time()

            # Check if we should restart.
            if self.autorestart and self._should_restart():
                self._do_restart()

            # Update status only if more than 10s or before a long sleep
            if time.time() > self.last_info_update + 10 or wait_time > 10:
                BernardStatus(checks=self.scheduler.checks,
                    schedule_count=self.scheduler.schedule_count).persist()
                self.last_info_update = time.time()

            # Only plan for the next loop if we will continue,
            # otherwise just exit quickly.
            if self.run_forever:
                # Give more time to the Watchdog because of the sleep
                StaticWatchdog.reset(int(wait_time))
                # Sleep until the next task schedule
                time.sleep(self.scheduler.wait_time())

        # Now clean-up.
        BernardStatus.remove_latest_status()

        # Explicitly kill the process, because it might be running
        # as a daemon.
        log.info("Exiting. Bye bye.")
        sys.exit(0)

    def _should_restart(self):
        if time.time() - self.agent_start > self.restart_interval:
            return True
        return False

    def _do_restart(self):
        log.info("Running an auto-restart.")
        sys.exit(AgentSupervisor.RESTART_EXIT_STATUS)

class BernardStatus(AgentStatus):

    NAME = 'Bernard'

    def __init__(self, checks=[], schedule_count=0):
        AgentStatus.__init__(self)
        self.check_stats = [check.get_status() for check in checks]
        self.schedule_count = schedule_count

        self.STATUS_COLOR = {S.OK: 'green', S.TIMEOUT: 'yellow', S.EXCEPTION: 'red', S.INVALID_OUTPUT: 'red'}
        self.STATE_COLOR = {R.OK: 'green', R.WARNING: 'yellow', R.CRITICAL: 'red', R.UNKNOWN: 'yellow', R.NONE: 'white'}

    def body_lines(self):
        lines = [
            "Schedule count: %s" % self.schedule_count,
            "Check count: %s" % len(self.check_stats),
        ]

        lines += [
            "",
            "Checks",
            "======",
            ""
        ]

        for check in self.check_stats:
            status_color = self.STATUS_COLOR[check['status']]
            state_color = self.STATE_COLOR[check['state']]
            lines += ['  %s: [%s] #%d run is %s' % (check['check_name'], style(check['status'], status_color),
                                                    check['run_count'], style(check['state'], state_color))]
            lines += ['    %s' % ((check['message'] or ' ').splitlines()[0])]

        return lines

    def has_error(self):
        return False

    def to_dict(self):
        status_info = AgentStatus.to_dict(self)
        check_stats = {
            'checks': self.check_stats,
            'schedule_count': self.schedule_count,
        }
        status_info.update(check_stats)

        return status_info

def get_bernard_config():
    """Return the configuration of Bernard"""

    osname = get_os()
    config_path = get_config_path(os_name=get_os(), filename=BERNARD_CONF)

    try:
        f = open(config_path)
    except (IOError, TypeError):
        log.info("Bernard isn't configured: can't find %s" % BERNARD_CONF)
        return {}
    try:
        bernard_config = yaml.load(f.read(), Loader=yLoader)
        assert bernard_config is not None
        f.close()
    except Exception:
        f.close()
        log.error("Unable to parse yaml config in %s" % config_path)
        return {}

    return bernard_config
