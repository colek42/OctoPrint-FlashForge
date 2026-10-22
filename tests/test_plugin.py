# coding=utf-8
# Run inside an OctoPrint virtualenv: python -m unittest discover -s tests
import os
import unittest

import octoprint_flashforge


class TemplateAutoescapeTest(unittest.TestCase):
	def test_declares_templates_autoescaped(self):
		# OctoPrint 1.13.0 enforces autoescaping and warns about plugins that do not opt in.
		plugin = octoprint_flashforge.FlashForgePlugin.__new__(octoprint_flashforge.FlashForgePlugin)
		self.assertTrue(plugin.is_template_autoescaped())

	def test_ships_no_templates(self):
		# Opting in is only a no-op while there are no Jinja templates to escape.
		package_dir = os.path.dirname(octoprint_flashforge.__file__)
		self.assertFalse(os.path.isdir(os.path.join(package_dir, "templates")))


if __name__ == "__main__":
	unittest.main()
