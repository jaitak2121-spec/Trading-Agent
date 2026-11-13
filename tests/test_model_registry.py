import json
import tempfile
import unittest
from pathlib import Path
from trading.adapters.persistence import SQLiteState, SQLiteAuditSink
from trading.app.model_registry import ModelRegistry, require_registry_approval
from trading.app.model_training import direction_model_sha256, paper_model_approval_phrase
from trading.core.audit import AuditLog
from trading.core.authz import Principal, Role


class RegistryTest(unittest.TestCase):
    def test_explicit_lifecycle_rejection_is_permanent_and_artifacts_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = {'schema':'offline-direction-model/v1', 'symbol':'BTC/USDT', 'exchange':'c2c1','interval_seconds':60,
                     'eligible_for_paper_validation': True, 'approved':False, 'paper_only':True,
                     'self_promoting':False, 'features':['return_1','return_3','range_fraction'],
                     'training_dataset_sha256':'b'*64, 'training_start_open_time':0,
                     'training_end_open_time':1000, 'means':[0,0,0], 'scales':[1,1,1],
                     'weights':[0,0,0], 'bias':0, 'threshold':0.5}
            identity = direction_model_sha256(model)
            evidence = {'schema':'offline-direction-evaluation/v1', 'model_sha256':identity,
                        'evaluation_dataset_sha256':'a'*64, 'samples':100,
                        'eligible_for_paper_trial':True, 'approved':False, 'paper_only':True,
                        'live_trading':False, 'self_promoting':False}
            model_path, evaluation_path = root/'model.json', root/'evaluation.json'
            model_path.write_text(json.dumps(model))
            evaluation_path.write_text(json.dumps(evidence))
            original = model_path.read_bytes()
            state = SQLiteState(root/'registry.db')
            try:
                sink = SQLiteAuditSink(state)
                registry = ModelRegistry(state, AuditLog(sink, initial_records=sink.records))
                operator = Principal('operator', Role.OPERATOR)
                registry.register(model_path, evaluation_path, operator, reason='test fixture')
                with self.assertRaises(ValueError):
                    registry.transition(identity, 'approved_for_paper', operator, reason='test')
                registry.transition(identity, 'approved_for_paper', operator, reason='test',
                                    confirmation=paper_model_approval_phrase(model))
                require_registry_approval(root/'registry.db', model, model_path, evaluation_path)
                saved = evaluation_path.read_bytes()
                evaluation_path.write_text('{}')
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    require_registry_approval(root/'registry.db', model, model_path, evaluation_path)
                evaluation_path.write_bytes(saved)
                registry.transition(identity, 'archived', operator, reason='test')
                registry.transition(identity, 'approved_for_paper', operator, reason='rollback',
                                    confirmation=paper_model_approval_phrase(model))
                registry.transition(identity, 'rejected', operator, reason='failed evidence')
                with self.assertRaisesRegex(ValueError, 'revoked'):
                    require_registry_approval(root/'registry.db', model, model_path, evaluation_path)
                with self.assertRaises(ValueError):
                    registry.transition(identity, 'approved_for_paper', operator, reason='test',
                                        confirmation=paper_model_approval_phrase(model))
                self.assertEqual(model_path.read_bytes(), original)
                self.assertEqual(registry.entries()[0]['state'], 'rejected')
                with self.assertRaises(ValueError):
                    registry.register(model_path, evaluation_path, operator, reason='replace')
            finally:
                state.close()
