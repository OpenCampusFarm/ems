"""Control the two allowed programs through the private supervisord socket."""

import http.client
import socket
import time
import xmlrpc.client

SOCKET = "/run/campus-ems/supervisor.sock"
PROGRAMS = {"fan": "fan", "ems": "core"}
STOPPED = {"STOPPED", "EXITED", "FATAL"}
NOT_RUNNING = 70
FAILED = 30


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


class UnixTransport(xmlrpc.client.Transport):
    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        if self._connection[0] != host:
            self._connection = (host, UnixHTTPConnection(host, timeout=self.timeout))
        return self._connection[1]


def _supervisor(timeout):
    return xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=UnixTransport(timeout))


def apply_mode(target, mode):
    program = PROGRAMS[target]
    with _supervisor(115) as rpc:
        try:
            if not rpc.supervisor.stopProcess(program, True):
                raise OSError(f"Supervisor did not confirm {program} stopped")
        except xmlrpc.client.Fault as error:
            if error.faultCode != NOT_RUNNING:
                raise
            if rpc.supervisor.getProcessInfo(program)["statename"].upper() not in STOPPED:
                raise OSError(f"{program} is still transitioning") from error
        if not (target == "ems" and mode == "off"):
            _start(rpc, program)


def _start(rpc, program):
    # Go supervisord can acknowledge a stop before clearing its start guard,
    # especially if the previous process lived for less than two seconds.
    deadline = time.monotonic() + 6
    while True:
        try:
            if rpc.supervisor.startProcess(program, True):
                return
            raise OSError(f"Supervisor did not confirm {program} started")
        except xmlrpc.client.Fault as error:
            if (error.faultCode != FAILED or time.monotonic() >= deadline
                    or rpc.supervisor.getProcessInfo(program)["statename"].upper() not in STOPPED):
                raise
            time.sleep(0.5)


def service_status(target):
    try:
        with _supervisor(3) as rpc:
            state = rpc.supervisor.getProcessInfo(PROGRAMS[target])["statename"].upper()
        return {
            "RUNNING": "Running", "STOPPED": "Stopped", "EXITED": "Stopped",
            "FATAL": "Failed", "BACKOFF": "Failed", "STARTING": "Starting",
            "STOPPING": "Stopping",
        }.get(state, "Unavailable")
    except (OSError, http.client.HTTPException, xmlrpc.client.Error):
        return "Unavailable"
