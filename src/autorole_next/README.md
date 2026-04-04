
Run TUI
```
python -m autorole_next.tui.run --db tmp/manual-seeder.db
```


Run Stage
```
python -m autorole_next run stage --stage formScraper --db tmp/manual-seeder.db
```


Update database
```
python scripts/update_queue_messages.py --db tmp/manual-seeder.db --set "status=queued,stage=formScraper" --where stage_name=formCompleter --apply


```
