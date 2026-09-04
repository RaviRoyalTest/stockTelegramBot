import unittest

from starlette.requests import Request

import dashboard


class DashboardHomepageTests(unittest.TestCase):
    def test_homepage_has_fallback_when_template_lookup_fails(self):
        original_templates = dashboard.templates

        class BrokenTemplates:
            def TemplateResponse(self, template_name, context):
                raise FileNotFoundError(f"missing template: {template_name}")

        dashboard.templates = BrokenTemplates()
        try:
            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
            })
            response = unittest.mock.AsyncMock() if False else None
            import asyncio
            response = asyncio.run(dashboard.read_index(request))
            self.assertEqual(response.status_code, 200)
            content = response.body.decode("utf-8")
            self.assertIn("Royal Stock", content)
            self.assertIn("Custom dashboard", content)
        finally:
            dashboard.templates = original_templates


if __name__ == "__main__":
    unittest.main()
