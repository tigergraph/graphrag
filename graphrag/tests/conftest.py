# Intentionally empty.
#
# A previous version of this file defined ``pytest_collection_modifyitems``
# that called ``config.hook.pytest_runtest_protocol`` for each item — that
# actually executed every test once during the collection phase and again
# during the normal runtest phase, so every test ran twice. The
# ``deselected_modules`` set it built was also never populated, so the
# hook had no useful effect besides the double-run.
