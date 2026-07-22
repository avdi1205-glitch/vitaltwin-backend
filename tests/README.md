# Backend-Tests

Dieses Verzeichnis wurde in **Etappe 2 (Twin Intelligence Core)** neu
angelegt — vorher gab es im gesamten Projekt keine automatisierten Tests
(siehe Etappe-1-Abschlussbericht).

## Ausführen

```powershell
cd backend
.\venv\Scripts\python.exe -m pytest tests\ -v
```

## Was hier getestet wird (und was nicht)

- **`test_validation.py`** — reine Funktionstests ohne Datenbank. Deckt die
  in Etappe 2 §6 geforderten Regeln ab (1-10-Skalen, Schlafdauer,
  Bewegungsminuten, Zukunftsdatum, Textlänge, Zeitzonen).
- **`test_auth.py`** — Tests für `core/auth.py` (Token-Extraktion,
  Ownership-Check `assert_owns`) mit einer gefälschten/minimalen
  Supabase-Antwort (kein echter Netzwerkzugriff, keine echten Zugangsdaten
  nötig).
- **Nicht enthalten:** echte Datenbank-Tests (Unique Constraints, doppelte
  Tageseinträge, tatsächliches Anlegen/Lesen/Löschen von Zeilen). Diese
  Session hatte keinen Zugriff auf ein echtes Supabase-Projekt
  (`SUPABASE_URL`/`SUPABASE_KEY` waren nicht gesetzt). Diese Tests müssen
  nachgeholt werden, sobald eine Test-Datenbank verfügbar ist — siehe
  `docs/DATA_ARCHITECTURE.md`, Abschnitt "Bekannte Testlücken".
