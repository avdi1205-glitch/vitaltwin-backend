-- Blog content seed (2026-08): 3 draft articles for the new /blog section.
-- These are DRAFTS (status='draft') — nothing here is publicly visible until
-- a founder/admin reviews the content and flips status to 'published' via
-- the existing Admin Control Center (Content Management tab,
-- /api/admin/content). No automatic publishing happens from this script.
--
-- Run once in the Supabase SQL Editor. Safe to re-run: uses a slug-based
-- upsert so running it twice won't create duplicates.

insert into public.vt_content_items (content_type, slug, title, body, status, created_by)
values
(
  'blog',
  'was-ist-ein-digitaler-wellness-zwilling',
  'Was ist ein digitaler Wellness-Zwilling?',
  'Der Begriff "digitaler Zwilling" stammt ursprünglich aus dem Ingenieurwesen: Ein digitaler Zwilling ist ein digitales Abbild eines realen Systems, das sich mit neuen Daten laufend aktualisiert und so ein möglichst genaues, aktuelles Bild dieses Systems liefert. Fabriken nutzen digitale Zwillinge zum Beispiel, um den Zustand einer Maschine zu überwachen, ohne ständig physisch vor Ort sein zu müssen.

VitalTwin überträgt diese Idee auf einen persönlichen Bereich: deinen Wellness-Alltag. Dein digitaler Wellness-Zwilling ist kein Sensor und kein Implantat, sondern ein strukturiertes, sich laufend aktualisierendes Profil, das ausschließlich aus den Werten entsteht, die du selbst freiwillig einträgst.

## Woraus besteht dein Wellness-Zwilling?

Dein Zwilling setzt sich aus mehreren Bausteinen zusammen, die du nach und nach befüllst:

- Tägliche Check-ins zu Stimmung, Energie, Stress und Schlafqualität
- Gewohnheiten, die du anlegst und über Serien verfolgst
- Wellness-Ziele, die du dir selbst setzt
- Optional: Biomarker wie HbA1c, CRP, Vitamin D oder ApoB für eine tiefere Twin-Berechnung
- Optional (Premium): Blutzuckerverläufe und Mahlzeiten aus dem Ernährungstagebuch

Je mehr du einträgst, desto differenzierter kann dein Zwilling Zusammenhänge erkennen — zum Beispiel, ob sich deine Schlafqualität verbessert, wenn du regelmäßiger Sport treibst, oder ob dein Energielevel mit deinem selbst eingeschätzten Stresslevel zusammenhängt.

## Was ein digitaler Wellness-Zwilling NICHT ist

Das ist mindestens genauso wichtig wie die Frage, was er ist. VitalTwin ist kein Medizinprodukt, stellt keine Diagnosen und ersetzt keine ärztliche Beratung. Die Auswertungen basieren ausschließlich auf den Daten, die du selbst einträgst — sie sind keine objektive, wissenschaftlich exakte Messung, sondern eine grobe Wellness-Orientierung auf Basis deiner eigenen Angaben. Wenn du wenige oder ungenaue Werte einträgst, sind auch die Einschätzungen entsprechend eingeschränkt. Das kommunizieren wir bewusst offen, statt Ergebnisse präziser wirken zu lassen, als sie es sind.

## Warum ein zusammenhängendes Profil sinnvoll ist

Die meisten Menschen nutzen für Schlaf, Bewegung und Ernährung unterschiedliche Apps — wenn überhaupt. Das Problem dabei: Diese Bereiche stehen selten isoliert nebeneinander, sie beeinflussen sich gegenseitig. Wer schlecht schläft, hat oft weniger Energie für Bewegung. Wer gestresst ist, isst häufig anders als sonst. Eine Einzel-App für Schlaf zeigt dir vielleicht deinen Schlafwert, aber nicht, wie er mit deiner restlichen Woche zusammenhängt.

VitalTwin bringt diese Bereiche an einem Ort zusammen und versucht, Muster sichtbar zu machen, die in getrennten Apps unsichtbar bleiben würden — nicht als automatisierte "Wahrheit", sondern als Ausgangspunkt für deine eigene Reflexion.

## Wie du dir deinen Wellness-Zwilling erschließt

Der praktische Einstieg ist einfach: Du legst ein kostenloses Konto an, trägst deinen ersten Check-in ein und beobachtest, wie sich dein Profil über Tage und Wochen entwickelt. Über "Frag deinen Twin" kannst du zusätzlich gezielte Fragen zu deiner eigenen Entwicklung stellen — die Antworten verweisen dabei immer auf die konkreten Daten, aus denen sie abgeleitet wurden, damit du nachvollziehen kannst, worauf eine Einschätzung beruht.

Wenn du erfahren möchtest, was VitalTwin heute bereits kann und woran wir aktuell noch arbeiten, findest du das ausführlich auf unserer Seite "Über uns".',
  'draft',
  'seed-script'
),
(
  'blog',
  'was-ist-cgm-glukoseverlaeufe-lesen',
  'Was ist CGM und wie liest man Glukoseverläufe?',
  'CGM steht für "Continuous Glucose Monitoring", auf Deutsch kontinuierliche Glukosemessung. Dabei misst ein kleiner Sensor, der meist am Oberarm oder Bauch getragen wird, den Glukosewert im Gewebe in kurzen Abständen — oft alle paar Minuten — und liefert so einen fortlaufenden Verlauf statt einzelner Momentaufnahmen, wie sie eine klassische Fingerstich-Messung liefert.

Ursprünglich wurde CGM-Technologie vor allem für Menschen mit Diabetes entwickelt, um die Blutzuckersteuerung im Alltag zu erleichtern. Inzwischen nutzen aber auch Menschen ohne Diagnose CGM-Sensoren, um mehr über die eigene Reaktion auf Mahlzeiten, Bewegung und Schlaf zu erfahren.

## Was ein Glukoseverlauf zeigen kann

Ein CGM-Verlauf zeigt in der Regel:

- Wie stark der Glukosewert nach einer Mahlzeit ansteigt (die sogenannte postprandiale Reaktion)
- Wie schnell der Wert nach einem Anstieg wieder in einen ruhigeren Bereich zurückkehrt
- Ob es über Nacht zu ungewöhnlichen Schwankungen kommt
- Wie unterschiedliche Mahlzeiten im Vergleich zueinander wirken

Diese Muster können ein interessanter Ausgangspunkt für die eigene Reflexion sein: Reagiert dein Körper auf ein bestimmtes Frühstück anders als auf ein anderes? Gibt es einen Zusammenhang zwischen einer stressigen Nacht und deinem Verlauf am nächsten Morgen?

## Was ein Glukoseverlauf NICHT ist

Ein einzelner Ausschlag im Verlauf ist keine Diagnose und für sich genommen meist wenig aussagekräftig. Glukosewerte schwanken bei jedem Menschen natürlicherweise, abhängig von Mahlzeitzusammensetzung, Bewegung, Schlaf, Stress und individuellen Faktoren. Eine seriöse Einordnung erfordert immer den Blick auf den Verlauf über Zeit — nicht auf einen einzelnen Ausreißer.

VitalTwin wertet importierte CGM-Daten im Ernährungstagebuch aus, um dir Muster verständlich darzustellen. Diese Auswertung ersetzt keine ärztliche Diagnostik und ist keine Einschätzung eines Diabetes-Risikos. Wenn du gesundheitliche Bedenken zu deinem Blutzucker hast, ist der richtige Ansprechpartner immer eine Ärztin oder ein Arzt, nicht eine App.

## So kannst du einen CGM-Verlauf für dich sinnvoll nutzen

1. Beobachte über mehrere Tage, nicht nur einen einzelnen Ausschlag.
2. Vergleiche ähnliche Situationen miteinander (z. B. zwei ähnliche Frühstücke an unterschiedlichen Tagen).
3. Ordne ungewöhnliche Werte in den Kontext ein — Schlaf, Stress und Bewegung wirken mit.
4. Nutze die Erkenntnisse als Gesprächsgrundlage, nicht als abschließendes Urteil über deine Gesundheit.

## CGM bei VitalTwin

Der CGM-Import und das Ernährungstagebuch sind Teil des Premium-Tarifs. Du importierst deine Messwerte, ordnest sie Mahlzeiten zu und siehst deinen Verlauf im Kontext deiner übrigen Wellness-Daten — als ein weiterer Baustein deines digitalen Wellness-Zwillings, nicht als isoliertes Zusatz-Tool.',
  'draft',
  'seed-script'
),
(
  'blog',
  'datenschutz-bei-wellness-und-gesundheits-apps',
  'Datenschutz bei Wellness- und Gesundheits-Apps: Worauf du achten solltest',
  'Wellness- und Gesundheits-Apps gehören zu den sensibelsten Kategorien von Apps überhaupt. Schlafdaten, Stimmung, Bewegungsverhalten oder Blutzuckerwerte lassen mitunter Rückschlüsse auf sehr persönliche Lebensumstände zu. Wer solche Apps nutzt, sollte wissen, worauf es beim Datenschutz wirklich ankommt — unabhängig davon, welche App man am Ende wählt.

## Warum Gesundheitsdaten besonders geschützt sind

Nach der Datenschutz-Grundverordnung (DSGVO) zählen Gesundheitsdaten zu den "besonderen Kategorien personenbezogener Daten" (Art. 9 DSGVO). Für ihre Verarbeitung gelten strengere Anforderungen als für gewöhnliche personenbezogene Daten: In der Regel ist eine ausdrückliche Einwilligung erforderlich, und die Verarbeitung muss auf das notwendige Maß beschränkt bleiben.

Das bedeutet: Eine seriöse Wellness-App sollte transparent machen, welche Daten sie verarbeitet, wofür genau, und wie lange sie gespeichert werden — nicht nur in einer langen, unkonkreten Datenschutzerklärung, sondern auch im Produkt selbst nachvollziehbar.

## Fragen, die du dir bei jeder Wellness-App stellen solltest

- Werden meine Daten verkauft oder an Werbenetzwerke weitergegeben?
- Wird mein Gesundheitsprofil für personalisierte Werbung verwendet?
- Kann ich meine Daten jederzeit exportieren?
- Kann ich mein Konto und meine Daten vollständig löschen lassen?
- Werden meine Daten an Dritte (z. B. KI-Anbieter) übermittelt, und wenn ja, welche genau?
- Gibt es eine klare Trennung zwischen notwendigen und optionalen Datenverarbeitungen (Consent-Management)?

Wenn eine App diese Fragen nicht klar beantworten kann oder will, ist das ein Warnsignal.

## Wie VitalTwin das umsetzt

Bei VitalTwin gilt: Gesundheits- und Wellness-Daten werden nicht verkauft und nicht für personalisierte Werbung genutzt. Jede Einwilligung (zum Beispiel zur KI-Nutzung oder zum Chat-Verlauf) ist ein eigener, granularer Zweck — kein pauschales "Ja zu allem". Jede Zustimmung wird protokolliert und lässt sich jederzeit widerrufen.

Im Profilbereich kannst du:

- deine vollständigen Daten als Export herunterladen,
- einzelne Datenkategorien gezielt löschen, ohne dein gesamtes Konto zu entfernen,
- eine vollständige Löschung deines Kontos anfordern (aus Sicherheitsgründen manuell geprüft, nicht automatisch sofort ausgeführt, um versehentlichen Datenverlust zu vermeiden).

Für die KI-Funktion "Frag deinen Twin" gilt zusätzlich: Es werden nur deine konkrete Anfrage sowie eine kompakte Zusammenfassung deiner eigenen, bereits eingetragenen Daten an einen externen KI-Anbieter übermittelt — niemals vollständige Datenbankinhalte und niemals Daten anderer Nutzer. Details dazu stehen in unseren KI-Hinweisen.

## Was du selbst tun kannst

Unabhängig von der gewählten App lohnt es sich, regelmäßig zu prüfen, welche Datenschutzeinstellungen und Einwilligungen aktiv sind, und nicht benötigte Datenverarbeitungen aktiv abzulehnen. Ein bewusster, informierter Umgang mit den eigenen Gesundheitsdaten beginnt nicht erst bei der App — sondern bei der Frage, welche Daten man überhaupt preisgeben möchte.

Mehr zu unserem eigenen Vorgehen findest du in unserer vollständigen Datenschutzerklärung.',
  'draft',
  'seed-script'
)
on conflict (content_type, slug) where slug is not null
do update set
  title = excluded.title,
  body = excluded.body,
  updated_at = now();
