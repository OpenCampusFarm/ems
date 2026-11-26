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
        original_pid = rpc.supervisor.getProcessInfo(program)["pid"]
        try:
            rpc.supervisor.stopProcess(program, True)
        except xmlrpc.client.Fault as error:
            if error.faultCode != NOT_RUNNING:
                raise
        if rpc.supervisor.getProcessInfo(program)["statename"].upper() not in STOPPED:
            raise OSError(f"Supervisor did not confirm {program} stopped")
        if not (target == "ems" and mode == "off"):
            _start(rpc, program, original_pid)


def _start(rpc, program, original_pid):
    # Older Go supervisord releases may acknowledge a start that was ignored.
    deadline = time.monotonic() + 8
    while True:
        info = rpc.supervisor.getProcessInfo(program)
        state = info["statename"].upper()
        if state == "RUNNING" and info["pid"] > 0 and info["pid"] != original_pid:
            return
        if time.monotonic() >= deadline:
            raise OSError(f"Supervisor did not confirm a new {program} process")
        if state in STOPPED:
            try:
                rpc.supervisor.startProcess(program, True)
            except xmlrpc.client.Fault as error:
                if error.faultCode != FAILED:
                    raise
        time.sleep(0.25)


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
