"""Stub of the one thing the flows use from data_entry_flow."""


class section:  # noqa: N801 - the name Home Assistant gives it
    """A collapsible group of fields; input for it arrives as a nested dict."""

    def __init__(self, schema, options=None):
        self.schema = schema
        self.options = options or {}

    def __call__(self, value):
        """Validate like the real one: voluptuous wants a callable here."""
        return self.schema(value)
