import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('mode', Path(__file__).resolve().parents[1]/'travis/acc_publication_mode.py')
mode=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mode)

class PublicationModeTests(unittest.TestCase):
    def route(self, selected, message, tag='6.1.1.8', selector='', actor='contributor'):
        return mode.resolve(selected,message,tag,'55',selector,actor,'rerunner')

    def test_explicit_preview_uses_development_version_and_provenance_only(self):
        for actor in ('contributor','noiro-tagger','noiro-generic','sumit'):
            result=self.route('preview','ACI release to test.pypi.org',actor=actor)
            self.assertEqual(result['version'],'6.1.1.8.dev55')
            self.assertEqual(result['index'],'testpypi')
            self.assertEqual(result['provenance'],{'actor':actor,'triggering_actor':'rerunner'})
            self.assertFalse(result['legacy_adapter'])

    def test_production_route_does_not_authorize_upload(self):
        result=self.route('production','Release to pypi.org')
        self.assertTrue(result['production_authorization_required'])
        self.assertEqual(result['version'],'6.1.1.8')
        self.assertEqual(result['index'],'pypi')

    def test_unknown_ambiguous_and_conflicting_modes_fail(self):
        for selected,message,selector in [
            ('','release to pypi.org',''),('unexpected','release to pypi.org',''),
            ('preview','release to pypi.org',''),('production','release to test.pypi.org',''),
            ('preview','release to pypi.org and release to test.pypi.org',''),
            ('production','no hint',''),('preview','release to test.pypi.org','noiro-tagger'),
            ('production','release to pypi.org','noiro-generic')]:
            with self.subTest(selected=selected,message=message,selector=selector):
                with self.assertRaises(mode.ModeError):self.route(selected,message,selector=selector)

    def test_absent_mode_preserves_old_gha_development_selector(self):
        for selector in ('noiro-tagger','noiro-generic'):
            result=self.route(None,'release to test.pypi.org',selector=selector)
            self.assertTrue(result['legacy_adapter'])
            self.assertEqual(result['version'],'6.1.1.8.dev55')
            self.assertEqual(result['legacy_entrypoint'],'push-to-pypi.sh')
            self.assertTrue(result['requires_legacy_authorization'])

    def test_absent_mode_keeps_signed_testpypi_and_rc_for_legacy_authorization(self):
        for tag in ('6.1.1.8','6.1.1.8rc3'):
            result=self.route(None,'release to test.pypi.org',tag=tag)
            self.assertEqual(result['version'],tag)
            self.assertFalse(result['development'])
            self.assertTrue(result['requires_legacy_authorization'])
        with self.assertRaises(mode.ModeError):self.route('preview','release to test.pypi.org',tag='6.1.1.8rc3')

    def test_legacy_production_keeps_existing_authorization_entrypoint(self):
        result=self.route(None,'release to pypi.org')
        self.assertTrue(result['legacy_adapter'])
        self.assertTrue(result['production_authorization_required'])
        self.assertEqual(result['version'],'6.1.1.8')

if __name__=='__main__':unittest.main()
