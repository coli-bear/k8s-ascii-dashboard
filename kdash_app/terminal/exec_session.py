"""POSIX PTY relay with resize, local disconnect and terminal restoration."""
import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty

# A fresh interpreter acquires the controlling terminal after setsid. This avoids
# preexec_fn/fork callbacks in the dashboard's multithreaded process.
_CHILD = """import fcntl, os, sys, termios
try:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])
except OSError as exc:
    print('KDASH: cannot start terminal command: ' + str(exc), file=sys.stderr)
    sys.exit(127)
"""


def exec_base(base):
    """Do not apply the inventory request deadline to a streaming session."""
    result, skip = [], False
    for value in base:
        if skip:
            skip = False
        elif value == '--request-timeout':
            skip = True
        elif not value.startswith('--request-timeout='):
            result.append(value)
    return result + ['--request-timeout=0']


def run(argv, first_output_timeout=None):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError('Exec needs an interactive terminal. Run ./kdash directly in a terminal.')
    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(input_fd)
    last_size = None
    master, slave = pty.openpty()
    proc = None
    reason = None
    old_output = False
    started = time.monotonic()

    def resize():
        nonlocal last_size
        try:
            size = fcntl.ioctl(input_fd, termios.TIOCGWINSZ, b'\0' * 8)
            if size != last_size:
                fcntl.ioctl(master, termios.TIOCSWINSZ, size)
                last_size = size
        except OSError:
            pass

    def write_all(fd, value):
        while value:
            written = os.write(fd, value)
            value = value[written:]

    try:
        mode = termios.tcgetattr(slave)
        mode[0] |= termios.ICRNL
        mode[1] |= termios.OPOST | termios.ONLCR
        mode[3] |= termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN
        termios.tcsetattr(slave, termios.TCSANOW, mode)
        resize()
        proc = subprocess.Popen([sys.executable, '-c', _CHILD] + list(argv), stdin=slave, stdout=slave,
                                stderr=slave, start_new_session=True, close_fds=True)
        os.close(slave)
        slave = None
        tty.setraw(input_fd)
        while True:
            if first_output_timeout is not None and not old_output and time.monotonic() - started >= first_output_timeout:
                reason = 'timeout'
                break
            ready, _, _ = select.select([master, input_fd], [], [], .1)
            # Poll size instead of replacing ncurses' native SIGWINCH handler.
            resize()
            if master in ready:
                try:
                    output = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not output:
                    break
                old_output = True
                write_all(output_fd, output)
            if input_fd in ready:
                value = os.read(input_fd, 65536)
                if not value:
                    reason = 'disconnected'
                    break
                if b'\x1d' in value:  # Ctrl+] is local; Escape belongs to the remote shell.
                    prefix = value.split(b'\x1d', 1)[0]
                    if prefix:
                        write_all(master, prefix)
                    reason = 'disconnected'
                    break
                write_all(master, value)
            if not ready and proc.poll() is not None:
                break
    finally:
        try:
            if proc is not None:
                # Closing the local stream also closes the kubectl session; it
                # does not make a claim about a remote command's exit status.
                if proc.poll() is None and reason is None:
                    try:
                        proc.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        pass
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                else:
                    proc.wait()
        finally:
            termios.tcsetattr(input_fd, termios.TCSANOW, saved)
            os.close(master)
            if slave is not None:
                os.close(slave)
    if reason == 'timeout':
        raise ValueError('No shell output within %ss; the local connection was closed. Check API/proxy connectivity and try another shell/container.' % first_output_timeout)
    if reason == 'disconnected':
        print('\nDisconnected from the container terminal.')
        return None
    return proc.returncode
