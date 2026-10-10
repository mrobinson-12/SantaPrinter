#!/usr/bin/env python3
"""Generate a private LAN TLS certificate; does not change network or services."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from santa_store import Store

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', required=True, help='Specific private IP of the Linux host')
parser.add_argument('--origin', required=True, help='For example https://192.168.1.20:8443')
parser.add_argument('--output', default='runtime/lan-tls')
args = parser.parse_args()
address, origin = ipaddress.ip_address(args.host), urlsplit(args.origin)
if not address.is_private or address.is_unspecified or origin.scheme != 'https' or not origin.hostname or not origin.port or origin.path or origin.query or origin.fragment or origin.username or origin.password:
    parser.error('Use a specific private bind IP and an HTTPS origin including its port.')
folder = Path(args.output).resolve()
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
if (folder / 'key.pem').exists() or (folder / 'cert.pem').exists():
    parser.error('Certificate already exists; choose a new output directory to rotate it.')
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, origin.hostname)])
try:
    alternative = x509.IPAddress(ipaddress.ip_address(origin.hostname))
except ValueError:
    alternative = x509.DNSName(origin.hostname)
now = datetime.now(timezone.utc)
cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)).add_extension(x509.SubjectAlternativeName([alternative]), critical=False).sign(key, hashes.SHA256())
Store.atomic_write(folder / 'key.pem', key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
Store.atomic_write(folder / 'cert.pem', cert.public_bytes(serialization.Encoding.PEM))
print('Trust this certificate on your owner devices before using LAN administration.')
print('Certificate SHA-256 fingerprint: ' + cert.fingerprint(hashes.SHA256()).hex())
print('\nAdd these settings to .env:')
for field, value in dict(SANTA_LAN_HOST=args.host, SANTA_LAN_PORT=origin.port, SANTA_LAN_ORIGIN=args.origin,
                         SANTA_LAN_CERT=folder / 'cert.pem', SANTA_LAN_KEY=folder / 'key.pem').items():
    print(str(field) + '=' + str(value))
print('\nRun .venv/bin/python santa_tools.py reset-owner-password before restarting.')
print('Restrict TCP ' + str(origin.port) + ' to your management LAN in the host firewall; do not route it through cloudflared.')
