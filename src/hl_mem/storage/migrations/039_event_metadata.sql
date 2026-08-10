-- Persist source-specific turn locators without mixing them into extractable content.
ALTER TABLE events ADD COLUMN metadata_json TEXT;
