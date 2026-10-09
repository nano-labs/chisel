import importlib


def load_exporter():
    # Dynamic import with a literal name: chisel sees it.
    return importlib.import_module("inventory.plugins.csv_export")


def load_by_name(name: str):
    # Built from a variable: invisible to any static tool.
    return importlib.import_module(f"inventory.plugins.{name}")
