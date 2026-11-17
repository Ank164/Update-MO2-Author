# Nexus Author Metadata

An MO2 2.5.3 / PyQt6 plugin for updating authors on installed mods.

Copy `nexus_author_metadata.py` into your MO2 `plugins` folder.

Restart MO2, select multiple installed mods in the left pane using Ctrl or Shift,
then right-click and choose **Update authors from Nexus / Dwemer**.

If any selected mods already have an author, an **Overwrite / Cancel** box appears
before lookups begin. Expand its details to see the mods and current authors.
**Overwrite** allows those authors to be replaced as part of this batch; **Cancel**
(also the default, Escape, or closing the box) stops the entire selection without
changes. If no selected mods have authors, this confirmation is not shown.

Uses MO2's existing Nexus login. Retrieves author fields from Nexus's
author value; the uploader is a separate field and is not substituted. Unresolved
mods are then checked on Dwemer Mods before manual entry is offered.

Dwemer lookups use a saved `url`, `website`, or `customURL` pointing to a Dwemer
mod page when available. Otherwise, the plugin searches using the installed mod
name and the archive filename from `installationFile`, if present. Automatic
name searches support Skyrim SE/VR; saved Dwemer links work independently of the
game. Only a unique exact title match (ignoring case, spaces and punctuation)
is accepted automatically. Version/ID suffixes are removed for searching but
never used to justify an automatic match. Ambiguous or partial matches open a
picker with **Enter author manually** as the default choice; Cancel cancels the
batch. Paginated search results always require confirmation. Each query checks
the first results page and the picker lists up to 20 candidates.

Dwemer author values come from **Created by**, never **Uploaded by** or the
page's uploader-based structured metadata. The plugin uses public pages without
a Dwemer login, so content hidden from anonymous visitors cannot be retrieved.
Requests run asynchronously with timeouts, a short delay and a cache for the
current batch. Access/rate-limit or connection errors stop further Dwemer
requests in that batch and leave unresolved mods for manual entry. Search sends
the selected mod names and archive-derived search terms to Dwemer Mods.

If neither source supplies an author, a box shows the mod name and lookup
problem so you can enter the author manually. **OK** applies it to the current mod and
asks for the next one. **All** applies that author to the current mod and all
remaining unresolved mods without more prompts. Authors entered with earlier OK
clicks are kept. **Cancel** (or closing the box) cancels the entire batch without
saving, including earlier entries and fetched results. A nonblank author is
required for OK or All. Existing authors are replaced only after the initial
overwrite confirmation. No mod contents are edited.

Nexus requests run sequentially, one per distinct game/mod ID. A login/access/rate-limit
error or timeout stops further Nexus lookups, then Dwemer lookups begin.
Cancelling the fetching progress window discards the entire batch
without opening manual-entry boxes. Completion is silent; reports record outcomes.

Before writing, the plugin waits for any already-running MO2 refresh. Each metadata
file is edited with Qt's own INI codec, verified and replaced atomically. If a mod
folder has no `meta.ini`, the plugin creates it when saving the author. It will
not replace a file created by another process during that save.
MO2 refreshes once afterward if any authors changed. Unrelated INI values are preserved, though Qt can
normalize formatting. Each author is checked against its initial value immediately
before writing; a value changed during the batch is preserved and reported instead
of being overwritten. Unchanged author values do not trigger a write.

Reports are under `plugins\data\nexus_author_metadata\<run>`.
No metadata backups are created.

Offline verification: set `MO2_DIR` to your MO2 installation directory and run
`test_plugin.py` with Python 3.12. It uses MO2's bundled
Qt and a stub host interface. Live MO2 menu integration and authenticated Nexus
requests were verified in MO2 2.5.3 beta 12. Automated tests cover the menu's
multiple selection, Nexus response handling, Dwemer parsing and matching,
candidate selection, batch cancellation, and metadata preservation. Live public
Dwemer search and creator extraction were also checked using the bundled Qt network stack.
