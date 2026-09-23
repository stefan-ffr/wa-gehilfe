// Felder, die beim Speichern von Schraegstrichen am Ende befreit werden.
const ADRESSFELDER = ['dienst'];
const FELDER = ['dienst', 'token', 'profil', 'cfKennung', 'cfGeheimnis'];

const STANDARD = {
  dienst: 'http://localhost:8099',
  profil: 'stefan',
  token: '',
  cfKennung: '',
  cfGeheimnis: '',
};

chrome.storage.sync.get(STANDARD)
  .then((w) => FELDER.forEach((f) => (document.getElementById(f).value = w[f])));

document.getElementById('speichern').addEventListener('click', async () => {
  const werte = Object.fromEntries(FELDER.map((f) => {
    let v = document.getElementById(f).value.trim();
    if (ADRESSFELDER.includes(f)) v = v.replace(/\/+$/, '');
    return [f, v];
  }));
  await chrome.storage.sync.set(werte);
  const s = document.getElementById('status');
  // Ehrlich bleiben: ohne Token laeuft nichts, das soll man hier schon sehen
  // und nicht erst als 401 mitten im Chat.
  s.textContent = werte.token
    ? 'Gespeichert.'
    : 'Gespeichert — aber ohne Zugangstoken weist der Dienst jede Anfrage ab.';
});
