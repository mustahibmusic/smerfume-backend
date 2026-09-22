"""
Parfumly catalogue metadata import.

Layers are deliberately separate so each can change or be replaced alone:

    client.py     HTTP only — talks to api.parfumly.in, returns raw JSON.
    normalize.py  Pure functions — raw JSON -> Smerfume-shaped dataclasses.
                  All seller/marketplace/commercial data is dropped here.
    importer.py   Plans (read-only) and applies (writes) the import against
                  the catalogue models, idempotently.
"""
