# Nexus Author Metadata

An MO2 2.5.3 / PyQt6 plugin for filling missing authors on installed mods.

Copy `nexus_author_metadata.py` into your MO2 `plugins` folder.

Restart MO2, select multiple installed mods in the left pane using Ctrl or Shift,
then right-click and choose **Fill missing authors from Nexus**.

Uses MO2's existing Nexus login. Fills only blank author fields from Nexus's
author value; the uploader is a separate field and is not substituted. Mods
without a valid Nexus mod ID and game are skipped. No mod contents are edited.

Requests run sequentially, one per distinct game/mod ID. A login/access/rate-limit
error stops further lookups. Cancel discards fetched results without applying them.
Timed-out requests stop the batch. Completion is silent; reports record outcomes.

Before writing, the plugin waits for any already-running MO2 refresh. Each metadata
file is edited with Qt's own INI codec, verified and replaced atomically.
MO2 refreshes once afterward if any authors changed. Unrelated INI values are preserved, though Qt can
normalize formatting. Existing authors are checked again immediately before writing.

Reports are under `plugins\data\nexus_author_metadata\<run>`.
No metadata backups are created.

Offline verification: set `MO2_DIR` to your MO2 installation directory and run
`test_plugin.py` with Python 3.12. It uses MO2's bundled
Qt and a stub host interface. Live MO2 menu integration and authenticated Nexus
requests were verified in MO2 2.5.3 beta 12. Automated tests cover the menu's
multiple selection, Nexus response handling, and metadata preservation.
