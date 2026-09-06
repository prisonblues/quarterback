"""Imports left behind when the fix pass rewrote the code that used them."""

import json
import os.path
from collections import Counter


def slug(name):
    return name.strip().lower()
