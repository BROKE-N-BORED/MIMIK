// Test fixture: the shape of upstream config.js (httptoolkit
// frida-interception-and-unpinning) that patch_config targets.
// NOT the real upstream file, just the three lines MIMIK patches
// plus a helper line proving the rest survives untouched.
const CERT_PEM = `-----BEGIN CERTIFICATE-----
MIIBplaceholder
-----END CERTIFICATE-----`;
const PROXY_HOST = '192.168.0.1';
const PROXY_PORT = 8080;

// Helper code the hooks depend on: patching must leave it alone.
function derToPem(der) { return der; }
