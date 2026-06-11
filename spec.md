Home Assistant Integration Smartes Batterie laden

Implementiere eine Home Assistant Integration für Smartes Heimbatterie laden zur optimalen Ausnutzung von dynamischen Stromtarifen
Dabei implementiere das möglichst so generisch, das dies für verschiedene Hersteller von Batterien, Ansteuerungen und Quellen von dynamischen Stromtarifen verwendet werden kann. 

Idee ist zu Zeiten von Dunkelflauten (später Herbst und Winter) bei sehr hohen Strompreisen zu bestimmten Tageszeiten die Hausbatterie ausreichend (anhand des ermittelten typischen Verbrauchs des Haushaltes) zu günstigen Strompreisen zu laden und dann die Energie aus dem Hausakku gezielt Energie abzugeben zu Zeiten mit wesentlich höheren Strompreisen (Offset einstellbar, vermutlich in Europa mindestens > 0,20 Euro/kWh). Dabei soll das ganze möglichst ideal erfolgen, um die bestmöglichste Ersparnis zu erzielen und den Hausenergiebedarf zu Spitzenlastzeiten ideal abzudecken. (neuronales Netz verwenden für die Optimierung?)

Wichtig ist die generische Integration, damit die Integration auch mit ganz anderen Setups und Komponenten verwendet werden kann. Vermutlich muss man dazu im Konfigurationsdialog der Integration die verschiedenen Quellen für Sensoren, Ansteuerungen, Strompreise usw. konfigurieren möglichst komfortabel konfigurieren koennen. Es soll aber auch nicht extrem komplex werden

Das Beispiel mit BYD HVM und Fronius kannst Du mit beschreibung, scripts und konfigurationen in die Dokumentation aufnehmen. Baue die Dokumentation so, das man dort auch weitere Beispiele für andere Setups aufnehmen kann.

Das ganze soll ein Github Projekt mit HACS Integration werden. Ein Projektname und das Logo sollen auch noch ergänzt werden

Informationen über das Haus / Energieverbrauch

Für die Ermittlung der optimalen Ladestrategie könnten folgende Informationen verwendet werden

- Wärmepumpe vorhanden ja/nein
- Wetterbericht
- Temperatursensor
- PV Produktion Vorhersage und PV Produktion ist
- Ladezustand des Heimakkus
- Energieverbrauch des Hauses mit genauer Verteilung
- Strompreis für die nächsten ca. 24-36h (europäische Strombörse)

Home Assistant Informationen

- in HA\_Energie\_Dokumentation.md ist eine Dokumentation zu finden in der das spezifische Setup beschrieben ist
- Home Assistant Zugriff ist (nur lesend) via Informationen in ~/claude/ha möglich

