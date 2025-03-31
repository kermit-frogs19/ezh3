from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from datetime import datetime, timedelta, UTC
from pathlib import Path
from cryptography.hazmat.backends import default_backend
from ipaddress import IPv4Address  # Import IPv4Address



def generate_self_signed_cert(certfile="cert.pem", keyfile="key.pem"):
    cert_path = Path(certfile)
    key_path = Path(keyfile)

    if cert_path.exists() and key_path.exists():
        print("[INFO] Using existing certificate.")
        return

    print("[INFO] Generating self-signed certificate using Python...")

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    # Generate self-signed certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now() - timedelta(hours=10))  # Set to 1 minute ago
        .not_valid_after(datetime.now() + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),  # Add DNS name
                x509.IPAddress(IPv4Address("127.0.0.1")),  # Add IP address
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256(), default_backend())
    )

    # Write private key to file
    with open(keyfile, "wb") as f:
        f.write(private_key.private_bytes(
            Encoding.PEM,
            PrivateFormat.TraditionalOpenSSL,
            NoEncryption()
        ))

    # Write certificate to file
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(Encoding.PEM))

    print("[INFO] Certificate generated successfully.")