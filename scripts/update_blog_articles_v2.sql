-- Content quality/language pass (2026-08): improves wording, adds proper
-- inline markdown (**bold**, [links](url)) now that the renderer supports
-- it, sets the `excerpt` column (added by migration 026) for a short
-- intro on both the blog list and article page. Updates the SAME 3
-- existing articles by slug — creates nothing new, replaces nothing else.
--
-- Run once in the Supabase SQL Editor. Idempotent (safe to re-run).

update public.vt_content_items set
  excerpt = 'Dein digitaler Wellness-Zwilling führt deine Schlaf-, Bewegungs- und Ernährungsdaten an einem Ort zusammen und zeigt dir deine eigene Entwicklung über Zeit — nicht nur einen allgemeinen Durchschnittswert.',
  body = 'Ein digitaler Zwilling ist ursprünglich ein Begriff aus dem Ingenieurwesen: ein digitales Abbild einer realen Maschine, das sich mit jeder neuen Messung aktualisiert und so ein möglichst genaues Bild ihres aktuellen Zustands liefert. VitalTwin überträgt diese Idee auf einen sehr persönlichen Bereich — deinen Wellness-Alltag.

Dein digitaler Wellness-Zwilling ist kein Sensor und kein Gerät, sondern ein Profil, das ausschließlich aus den Werten entsteht, die du selbst einträgst. Es aktualisiert sich mit jedem Check-in und wird mit der Zeit differenzierter.

## Verschiedene Wellness-Bereiche an einem Ort

Schlaf, Bewegung, Ernährung und Stimmung stehen selten für sich allein. Wer schlecht schläft, hat oft weniger Energie für Bewegung. Wer gestresst ist, isst häufig anders als sonst. Die meisten Apps zeigen dir trotzdem nur einen dieser Bereiche auf einmal — VitalTwin führt sie zusammen, damit Zusammenhänge überhaupt erst sichtbar werden können.

## Deine eigene Baseline statt allgemeiner Durchschnittswerte

Ein Wert wie **"7 Stunden Schlaf"** sagt für sich genommen wenig aus. Interessant wird es erst im Vergleich zu deinen eigenen, typischen Werten: Schläfst du gerade schlechter als sonst? Bewegst du dich weniger als in den Wochen zuvor? VitalTwin vergleicht dich mit dir selbst, nicht mit einem anonymen Durchschnitt, der für deine Situation ohnehin wenig bedeutet.

## Entwicklung über Zeit statt Momentaufnahme

Ein einzelner Tag sagt wenig über deine Gewohnheiten aus. Erst der Verlauf über Wochen zeigt, ob sich etwas wirklich verändert — dafür gibt es den Wochenrückblick und den Verlauf im Dashboard.

## Transparente Unsicherheit statt falscher Genauigkeit

Alle Auswertungen basieren ausschließlich auf den Daten, die du einträgst. Trägst du wenig oder unregelmäßig ein, sagt dir VitalTwin das auch so — lieber "noch nicht genug Daten" als eine Zahl, die mehr Sicherheit vorgaukelt, als tatsächlich vorhanden ist.

## Was VitalTwin nicht ist

VitalTwin ist ein Wellness-Tool zur Selbstreflexion, **kein Medizinprodukt** und keine Diagnose. Die Plattform ersetzt keinen Arztbesuch und trifft keine medizinischen Einschätzungen.

## Heute verfügbar, in Entwicklung und Zukunftsperspektive

**Heute verfügbar**: manuelle Check-ins zu Schlaf, Bewegung, Ernährung und Stimmung, Gewohnheiten- und Zielverfolgung, Wochenrückblick, sowie "Frag deinen Twin" als KI-gestützter Assistent auf Basis deiner eigenen Daten.

**Aktuell in Entwicklung**: eine tiefere Anbindung an Wearables über Google Health.

**Langfristige Vision**: möglichst automatische Datenübernahme aus verbundenen Quellen, damit du perspektivisch weniger manuell eintragen musst — ohne dass sich am Grundprinzip etwas ändert: Dein Zwilling gehört dir, und du entscheidest, welche Daten einfließen.

Mehr zur Idee hinter VitalTwin findest du auf [unserer Über-uns-Seite](/ueber-uns).',
  updated_at = now()
where content_type = 'blog' and slug = 'was-ist-ein-digitaler-wellness-zwilling';

update public.vt_content_items set
  excerpt = 'CGM misst deinen Glukoseverlauf kontinuierlich statt punktuell. Was die Kurven wirklich zeigen können — und wo die Grenzen liegen.',
  body = 'CGM steht für "Continuous Glucose Monitoring", auf Deutsch kontinuierliche Glukosemessung. Ein kleiner Sensor, meist am Oberarm oder Bauch getragen, misst den Glukosewert im Gewebe in kurzen Abständen und liefert so einen fortlaufenden Verlauf — statt der einzelnen Momentaufnahme, die eine klassische Fingerstich-Messung liefert.

Ursprünglich wurde die Technologie vor allem für Menschen mit Diabetes entwickelt. Inzwischen nutzen auch Menschen ohne Diagnose CGM-Sensoren, um mehr über ihre Reaktion auf Mahlzeiten, Bewegung und Schlaf zu erfahren.

## Was ein Glukoseverlauf zeigen kann

- Wie stark der Wert nach einer Mahlzeit ansteigt
- Wie schnell er danach wieder in einen ruhigeren Bereich zurückkehrt
- Ob es über Nacht zu ungewöhnlichen Schwankungen kommt
- Wie sich verschiedene Mahlzeiten im Vergleich zueinander auswirken

Solche Muster können ein interessanter Ausgangspunkt für die eigene Reflexion sein: Reagierst du auf ein bestimmtes Frühstück anders als auf ein anderes? Gibt es einen Zusammenhang zwischen einer unruhigen Nacht und dem Verlauf am nächsten Morgen?

## Was ein Glukoseverlauf nicht ist

Ein einzelner Ausschlag im Verlauf ist **keine Diagnose** und für sich genommen meist wenig aussagekräftig. Glukosewerte schwanken bei jedem Menschen natürlicherweise — abhängig von Mahlzeit, Bewegung, Schlaf, Stress und individuellen Faktoren. Eine seriöse Einordnung braucht immer den Blick auf den Verlauf über Zeit, nicht auf einen einzelnen Ausreißer.

VitalTwin wertet importierte CGM-Daten im Ernährungstagebuch aus, um dir Muster verständlich darzustellen. Das ersetzt keine ärztliche Diagnostik und ist keine Einschätzung eines Diabetes-Risikos. Bei gesundheitlichen Bedenken zu deinem Blutzucker ist der richtige Ansprechpartner immer eine Ärztin oder ein Arzt, nicht eine App.

## So liest du deinen Verlauf sinnvoll

1. Beobachte über mehrere Tage, nicht nur einen einzelnen Ausschlag.
2. Vergleiche ähnliche Situationen miteinander, etwa zwei ähnliche Frühstücke an unterschiedlichen Tagen.
3. Ordne ungewöhnliche Werte in den Kontext ein — Schlaf, Stress und Bewegung wirken mit.
4. Nutze die Erkenntnisse als Gesprächsgrundlage, nicht als abschließendes Urteil über deine Gesundheit.

## CGM bei VitalTwin — heute verfügbar

Der CGM-Import und das Ernährungstagebuch sind Teil des [Premium-Tarifs](/preise). Du importierst deine Messwerte, ordnest sie Mahlzeiten zu und siehst deinen Verlauf im Kontext deiner übrigen Wellness-Daten — als ein weiterer Baustein deines digitalen Wellness-Zwillings, nicht als isoliertes Zusatz-Tool.',
  updated_at = now()
where content_type = 'blog' and slug = 'was-ist-cgm-glukoseverlaeufe-lesen';

update public.vt_content_items set
  excerpt = 'Gesundheitsdaten gehören zu den sensibelsten Informationen überhaupt. Worauf du bei jeder Wellness-App achten solltest — und wie VitalTwin es konkret umsetzt.',
  body = 'Wellness- und Gesundheits-Apps gehören zu den sensibelsten App-Kategorien überhaupt. Schlafdaten, Stimmung, Bewegungsverhalten oder Blutzuckerwerte lassen mitunter Rückschlüsse auf sehr persönliche Lebensumstände zu. Wer solche Apps nutzt, sollte wissen, worauf es beim Datenschutz wirklich ankommt — unabhängig davon, welche App man am Ende wählt.

## Warum Gesundheitsdaten besonders geschützt sind

Nach der Datenschutz-Grundverordnung (DSGVO) zählen Gesundheitsdaten zu den "besonderen Kategorien personenbezogener Daten" (Art. 9 DSGVO). Für ihre Verarbeitung gelten strengere Regeln als für gewöhnliche personenbezogene Daten: In der Regel ist eine ausdrückliche Einwilligung nötig, und die Verarbeitung muss auf das notwendige Maß beschränkt bleiben.

Eine seriöse Wellness-App sollte transparent machen, welche Daten sie verarbeitet, wofür genau, und wie lange sie gespeichert werden — nicht nur in einer langen Datenschutzerklärung, sondern auch im Produkt selbst nachvollziehbar.

## Fragen, die du dir bei jeder Wellness-App stellen solltest

- Werden meine Daten verkauft oder an Werbenetzwerke weitergegeben?
- Wird mein Gesundheitsprofil für personalisierte Werbung genutzt?
- Kann ich meine Daten jederzeit exportieren?
- Kann ich mein Konto und meine Daten vollständig löschen lassen?
- Werden meine Daten an Dritte übermittelt — und wenn ja, an wen genau?
- Gibt es eine klare Trennung zwischen notwendigen und optionalen Datenverarbeitungen?

Wenn eine App diese Fragen nicht klar beantworten kann oder will, ist das ein Warnsignal.

## Wie VitalTwin das umsetzt

Gesundheits- und Wellness-Daten werden bei VitalTwin **nicht verkauft** und nicht für personalisierte Werbung genutzt. Jede Einwilligung — etwa zur KI-Nutzung oder zum Chat-Verlauf — ist ein eigener, klar abgegrenzter Zweck statt eines pauschalen "Ja zu allem". Jede Zustimmung wird protokolliert und lässt sich jederzeit widerrufen.

Im [Profilbereich](/profil) kannst du:

- deine vollständigen Daten als Export herunterladen,
- einzelne Datenkategorien gezielt löschen, ohne dein gesamtes Konto zu entfernen,
- eine vollständige Löschung deines Kontos anfordern (aus Sicherheitsgründen manuell geprüft, nicht automatisch sofort ausgeführt).

Für "Frag deinen Twin" gilt zusätzlich: Es werden nur deine konkrete Anfrage sowie eine kompakte Zusammenfassung deiner eigenen, bereits eingetragenen Daten an einen externen KI-Anbieter übermittelt — niemals vollständige Datenbankinhalte und niemals Daten anderer Nutzer. Details dazu stehen in unseren [KI-Hinweisen](/ki-hinweise).

## Was du selbst tun kannst

Unabhängig von der gewählten App lohnt es sich, regelmäßig zu prüfen, welche Einwilligungen aktiv sind, und nicht benötigte Datenverarbeitungen aktiv abzulehnen. Ein bewusster Umgang mit den eigenen Gesundheitsdaten beginnt nicht erst bei der App, sondern bei der Frage, welche Daten man überhaupt preisgeben möchte.

Mehr zu unserem eigenen Vorgehen findest du in unserer vollständigen [Datenschutzerklärung](/datenschutz).',
  updated_at = now()
where content_type = 'blog' and slug = 'datenschutz-bei-wellness-und-gesundheits-apps';
