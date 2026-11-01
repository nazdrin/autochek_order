import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supplier4_run_order as supplier4


class AccountLockTests(unittest.TestCase):
    def test_posix_lock_blocks_second_holder(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(supplier4.tempfile, 'gettempdir', return_value=folder):
            with supplier4._account_lock():
                with self.assertRaises(supplier4.StageError):
                    with supplier4._account_lock():
                        self.fail('Second holder acquired the lock')
            with supplier4._account_lock():
                pass

    def test_windows_locks_and_unlocks_one_byte(self):
        locking = Mock()
        module = SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2)
        with tempfile.TemporaryDirectory() as folder, patch.object(supplier4.tempfile, 'gettempdir', return_value=folder), patch.object(supplier4, 'os', SimpleNamespace(name='nt')), patch.dict(sys.modules, {'msvcrt': module}):
            with supplier4._account_lock():
                self.assertEqual(Path(folder, 'supplier4_monsterlab_account.lock').read_bytes(), b'0')
        self.assertEqual([call.args[1:] for call in locking.call_args_list], [(1, 1), (2, 1)])

    def test_windows_busy_lock_stops_processing(self):
        module = SimpleNamespace(locking=Mock(side_effect=OSError('busy')), LK_NBLCK=1, LK_UNLCK=2)
        with tempfile.TemporaryDirectory() as folder, patch.object(supplier4.tempfile, 'gettempdir', return_value=folder), patch.object(supplier4, 'os', SimpleNamespace(name='nt')), patch.dict(sys.modules, {'msvcrt': module}):
            with self.assertRaises(supplier4.StageError):
                with supplier4._account_lock():
                    self.fail('Busy account was processed')
