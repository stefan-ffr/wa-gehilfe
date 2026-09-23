/* Service Worker -- bewusst so wenig wie moeglich.
 *
 * Er existiert, weil ein installierbares Web-App-Manifest einen verlangt.
 * Zwischenspeichern tut er NICHT: hinter jeder Seite steht eine Anmeldung
 * und ein Verlauf, der sich staendig aendert. Ein Cache haette hier zwei
 * Wirkungen, beide schlecht -- veraltete Nachrichten und Inhalte, die nach
 * dem Abmelden noch auf dem Geraet liegen.
 *
 * Also: durchreichen, sonst nichts. Faellt das Netz aus, sagt der Browser
 * das selbst, und zwar ehrlicher als ein alter Zwischenstand.
 */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => { /* absichtlich leer */ });
