package org.lazyremote.tv;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Base64;
import java.util.Date;
import java.util.concurrent.TimeUnit;

import org.bouncycastle.asn1.x500.X500Name;
import org.bouncycastle.asn1.x509.BasicConstraints;
import org.bouncycastle.asn1.x509.Extension;
import org.bouncycastle.asn1.x509.ExtendedKeyUsage;
import org.bouncycastle.asn1.x509.GeneralName;
import org.bouncycastle.asn1.x509.GeneralNames;
import org.bouncycastle.asn1.x509.GeneralSubtree;
import org.bouncycastle.asn1.x509.KeyPurposeId;
import org.bouncycastle.asn1.x509.KeyUsage;
import org.bouncycastle.asn1.x509.NameConstraints;
import org.bouncycastle.cert.jcajce.JcaX509ExtensionUtils;
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder;
import org.bouncycastle.operator.OperatorCreationException;
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder;

final class CertificateFiles {
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final X500Name ROOT = new X500Name("CN=Lazy Remote TV Root");
    private static final X500Name ISSUER = new X500Name("CN=Lazy Remote TV LAN Issuer");

    private CertificateFiles() {}

    static void issuer(String directory) throws GeneralSecurityException, IOException, OperatorCreationException {
        KeyPairGenerator keys = rsa();
        KeyPair root = keys.generateKeyPair();
        KeyPair issuer = keys.generateKeyPair();
        var rootCertificate = certificate(ROOT, ROOT, root, root, 1825, 1);
        writePem(directory, "root.pem", "CERTIFICATE", rootCertificate.build(signer(root.getPrivate())).getEncoded());

        var issuerCertificate = certificate(ROOT, ISSUER, issuer, root, 1825, 0);
        GeneralSubtree[] privateNetworks = {
            network("10.0.0.0/8"), network("172.16.0.0/12"), network("192.168.0.0/16"),
            network("127.0.0.1/32"),
        };
        issuerCertificate.addExtension(Extension.nameConstraints, true, new NameConstraints(privateNetworks, null));
        writePem(directory, "issuer.pem", "CERTIFICATE", issuerCertificate.build(signer(root.getPrivate())).getEncoded());
        writePem(directory, "issuer.key", "PRIVATE KEY", issuer.getPrivate().getEncoded());
    }

    static void leaf(String directory, String address)
            throws GeneralSecurityException, IOException, OperatorCreationException {
        KeyPair server = rsa().generateKeyPair();
        X509Certificate issuer = read(new File(directory, "issuer.pem"));
        String pem = new String(Files.readAllBytes(new File(directory, "issuer.key").toPath()), StandardCharsets.US_ASCII);
        byte[] encodedKey = Base64.getMimeDecoder().decode(
                pem.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", ""));
        PrivateKey key = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(encodedKey));
        var leaf = certificate(ISSUER, new X500Name("CN=Lazy Remote TV"), server,
                new KeyPair(issuer.getPublicKey(), key), 90, -1);
        leaf.addExtension(Extension.subjectAlternativeName, false,
                new GeneralNames(new GeneralName(GeneralName.iPAddress, address)));
        writePem(directory, "server.pem", "CERTIFICATE", leaf.build(signer(key)).getEncoded());
        writePem(directory, "server.key", "PRIVATE KEY", server.getPrivate().getEncoded());
    }

    static boolean expires(String path, int withinDays) throws GeneralSecurityException, IOException {
        return read(new File(path)).getNotAfter().getTime()
                <= System.currentTimeMillis() + TimeUnit.DAYS.toMillis(withinDays);
    }

    private static X509Certificate read(File file) throws GeneralSecurityException, IOException {
        try (InputStream input = Files.newInputStream(file.toPath())) {
            return (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(input);
        }
    }

    private static JcaX509v3CertificateBuilder certificate(X500Name issuerName, X500Name subject, KeyPair subjectKey,
            KeyPair issuerKey, int days, int pathLength) throws GeneralSecurityException, IOException {
        long now = System.currentTimeMillis();
        boolean ca = pathLength >= 0;
        var extensions = new JcaX509ExtensionUtils();
        var certificate = new JcaX509v3CertificateBuilder(issuerName, new BigInteger(159, RANDOM).setBit(158),
                new Date(now - TimeUnit.MINUTES.toMillis(5)), new Date(now + TimeUnit.DAYS.toMillis(days)),
                subject, subjectKey.getPublic());
        certificate.addExtension(Extension.basicConstraints, true,
                ca ? new BasicConstraints(pathLength) : new BasicConstraints(false));
        certificate.addExtension(Extension.keyUsage, true, new KeyUsage(ca
                ? KeyUsage.keyCertSign | KeyUsage.cRLSign : KeyUsage.digitalSignature | KeyUsage.keyEncipherment));
        certificate.addExtension(Extension.subjectKeyIdentifier, false,
                extensions.createSubjectKeyIdentifier(subjectKey.getPublic()));
        if (!subject.equals(issuerName)) {
            certificate.addExtension(Extension.authorityKeyIdentifier, false,
                    extensions.createAuthorityKeyIdentifier(issuerKey.getPublic()));
            certificate.addExtension(Extension.extendedKeyUsage, false,
                    new ExtendedKeyUsage(KeyPurposeId.id_kp_serverAuth));
        }
        return certificate;
    }

    private static GeneralSubtree network(String cidr) {
        return new GeneralSubtree(new GeneralName(GeneralName.iPAddress, cidr));
    }

    private static KeyPairGenerator rsa() throws GeneralSecurityException {
        KeyPairGenerator keys = KeyPairGenerator.getInstance("RSA");
        keys.initialize(2048, RANDOM);
        return keys;
    }

    private static org.bouncycastle.operator.ContentSigner signer(PrivateKey key) throws OperatorCreationException {
        return new JcaContentSignerBuilder("SHA256withRSA").build(key);
    }

    private static void writePem(String directory, String file, String type, byte[] bytes) throws IOException {
        String body = Base64.getMimeEncoder(64, new byte[]{ '\n' }).encodeToString(bytes);
        String pem = "-----BEGIN " + type + "-----\n" + body + "\n-----END " + type + "-----\n";
        Files.write(new File(directory, file).toPath(), pem.getBytes(StandardCharsets.US_ASCII));
    }
}
