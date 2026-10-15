import unittest
from unittest.mock import Mock,patch
from kdash_app.resources import resource_actions as actions
def pod():
    return dict(apiVersion='v1',kind='Pod',metadata=dict(name='web',namespace='prod',uid='original'),spec=dict(containers=[dict(name='app')]),status=dict(phase='Running',containerStatuses=[dict(name='app',state=dict(running={}))]))


class ExecTerminalTests(unittest.TestCase):
    def test_shell_menu_uses_interactive_shell_and_separate_request_timeout(self):
        c=Mock();c.base=['kubectl','--kubeconfig','/synthetic.conf','--request-timeout=8s','--context','synthetic'];c.call.return_value=(pod(),None)
        original=list(c.base)
        with patch('kdash_app.resources.resource_actions.profiles.prompt',side_effect=['bash','']),patch('kdash_app.resources.resource_actions.terminal_exec.run',return_value=0) as run,patch('builtins.print'):
            actions.exec_command(c,dict(resource='pods',name='web',namespace='prod',namespaced=True,uid='original'))
        argv=run.call_args.args[0]
        self.assertEqual(argv[-3:],['--','/bin/bash','-i'])
        self.assertIn('--request-timeout=0',argv)
        self.assertNotIn('--request-timeout=8s',argv)
        self.assertEqual(c.base,original)

    def test_escape_cancels_shell_without_launching(self):
        from kdash_app.config import profiles as profiles
        c=Mock();c.call.return_value=(pod(),None)
        with patch('kdash_app.resources.resource_actions.profiles.prompt',side_effect=profiles.InputCancelled()),patch('kdash_app.resources.resource_actions.terminal_exec.run') as run,patch('builtins.print'):
            with self.assertRaises(profiles.InputCancelled):actions.exec_command(c,dict(resource='pods',name='web',namespace='prod',namespaced=True,uid='original'))
        run.assert_not_called()

    def test_replacement_while_confirming_never_executes(self):
        c=Mock();c.base=['kubectl'];replacement=pod();replacement['metadata']['uid']='replacement'
        c.call.side_effect=[(pod(),None),(replacement,None)]
        with patch('kdash_app.resources.resource_actions.profiles.prompt',side_effect=['sh','']),patch('kdash_app.resources.resource_actions.terminal_exec.run') as run,patch('builtins.print'):
            with self.assertRaisesRegex(ValueError,'replaced'):actions.exec_command(c,dict(resource='pods',name='web',namespace='prod',namespaced=True,uid='original'))
        run.assert_not_called()


class LocalPTYTests(unittest.TestCase):
    """Real local PTYs and shells only: no kubectl, kubeconfig or cluster."""
    def session(self, argv, feed=None, first_timeout=2, resize=False):
        import errno,fcntl,os,pty,select,struct,subprocess,sys,termios,time
        from kdash_app.terminal import exec_session as terminal_exec
        master,slave=pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',33,120,0,0))
        script=("import sys,termios;sys.path.insert(0,"+repr(str(__import__('pathlib').Path(terminal_exec.__file__).parents[2]))+")\n"
                "from kdash_app.terminal import exec_session as terminal_exec\nsaved=termios.tcgetattr(0)\ntry:\n"
                " result=terminal_exec.run("+repr(argv)+",first_output_timeout="+repr(first_timeout)+")\n print('RESULT',repr(result),flush=True)\n"
                "except ValueError as exc:\n print('ERROR',str(exc),flush=True)\n"
                "finally:\n restored=termios.tcgetattr(0);restored[3]&=~getattr(termios,'PENDIN',0);saved[3]&=~getattr(termios,'PENDIN',0);print('RESTORED',restored==saved,flush=True)\n")
        proc=subprocess.Popen([sys.executable,'-c',script],stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave)
        output=bytearray();sent=False;deadline=time.monotonic()+7
        try:
            while time.monotonic()<deadline:
                ready,_,_=select.select([master],[],[],.05)
                if ready:
                    try:data=os.read(master,65536)
                    except OSError as exc:
                        if exc.errno==errno.EIO:break
                        raise
                    if not data:break
                    output.extend(data)
                if not sent and feed and feed[0] in output:
                    if resize:
                        fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',41,135,0,0))
                    os.write(master,feed[1]);sent=True
                if b'RESTORED' in output and proc.poll() is not None:break
            self.assertIsNotNone(proc.poll() if proc.poll() is not None else self.wait(proc),output.decode(errors='replace'))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=3)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
            os.close(master)
        return output.decode(errors='replace')

    def wait(self,proc):
        import subprocess
        try:return proc.wait(timeout=.5)
        except subprocess.TimeoutExpired:return None

    def test_real_local_sh_and_bash_round_trip_and_restore(self):
        import sys
        from pathlib import Path
        for executable in ('/bin/sh','/bin/bash'):
            if not Path(executable).exists():continue
            options=['--noprofile','--norc','-i'] if executable.endswith('bash') else ['-i']
            child="import os;os.environ.pop('ENV',None);os.environ.pop('BASH_ENV',None);os.environ['HISTFILE']='/dev/null';os.environ['BASH_SILENCE_DEPRECATION_WARNING']='1';os.environ['PS1']='KDASH_LOCAL_READY> ';os.execv("+repr(executable)+","+repr([executable]+options)+")"
            with self.subTest(shell=executable):
                output=self.session([sys.executable,'-c',child],feed=(b'KDASH_LOCAL_READY> ',b"printf 'LOCAL_SHELL_OK\\n'; exit 7\n"))
                self.assertIn('LOCAL_SHELL_OK',output);self.assertIn('RESULT 7',output);self.assertIn('RESTORED True',output)

    def test_child_has_controlling_terminal_and_receives_resize(self):
        import sys
        child="import os,fcntl,termios,struct;print('TTY',os.isatty(0),'CONTROL',os.tcgetpgrp(0)==os.getpgrp(),flush=True);print('SIZE1',struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))[:2],flush=True);print('KDASH_CHILD_READY',flush=True);input();print('SIZE2',struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))[:2],flush=True)"
        output=self.session([sys.executable,'-c',child],feed=(b'KDASH_CHILD_READY',b'go\n'),resize=True)
        self.assertIn('TTY True CONTROL True',output);self.assertIn('SIZE1 (33, 120)',output);self.assertIn('SIZE2 (41, 135)',output);self.assertIn('RESTORED True',output)

    def test_silent_startup_times_out_and_restores_terminal(self):
        import sys
        output=self.session([sys.executable,'-c','import time;time.sleep(30)'],first_timeout=.15)
        self.assertIn('No shell output',output);self.assertIn('RESTORED True',output)

    def test_local_disconnect_returns_without_waiting_for_remote_output(self):
        import sys
        output=self.session([sys.executable,'-c',"import time;print('KDASH_CHILD_READY',flush=True);time.sleep(30)"],feed=(b'KDASH_CHILD_READY',b'\x1d'))
        self.assertIn('Disconnected from',output);self.assertIn('RESULT None',output);self.assertIn('RESTORED True',output)

    def test_request_timeout_copy_and_noninteractive_rejection(self):
        from kdash_app.terminal import exec_session as terminal_exec
        original=['kubectl','--request-timeout','8s','--context','synthetic']
        self.assertEqual(terminal_exec.exec_base(original),['kubectl','--context','synthetic','--request-timeout=0'])
        self.assertEqual(original[1:3],['--request-timeout','8s'])
        with patch('kdash_app.terminal.exec_session.sys.stdin.isatty',return_value=False):
            with self.assertRaisesRegex(ValueError,'interactive terminal'):terminal_exec.run(['synthetic'])

    def test_ctrl_c_interrupts_a_local_child_and_restores_terminal(self):
        import sys
        output=self.session([sys.executable,'-c',"import time;print('KDASH_CHILD_READY',flush=True);time.sleep(30)"],feed=(b'KDASH_CHILD_READY',b'\x03'))
        self.assertIn('RESTORED True',output)
        self.assertIn('KeyboardInterrupt',output)

    def test_curses_shell_mode_and_program_mode_restore_on_callback_error(self):
        from kdash_app.ui import dashboard as kdash
        terminal=Mock();events=[]
        def fail():
            events.append('callback')
            raise RuntimeError('synthetic failure')
        with patch('kdash_app.ui.dashboard.curses.def_prog_mode'),patch('kdash_app.ui.dashboard.curses.endwin'),patch('kdash_app.ui.dashboard.curses.reset_shell_mode',side_effect=lambda:events.append('shell')),patch('kdash_app.ui.dashboard.curses.reset_prog_mode',side_effect=lambda:events.append('program')),patch('kdash_app.ui.dashboard.curses.curs_set'),patch('kdash_app.ui.dashboard.sys.stdout'):
            with self.assertRaisesRegex(RuntimeError,'synthetic failure'):kdash.terminal_action(terminal,fail)
        self.assertEqual(events,['shell','callback','program'])
        terminal.clearok.assert_called_once_with(True)

if __name__=='__main__':unittest.main()
