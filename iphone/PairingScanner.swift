import AVFoundation
import SwiftUI
import VisionKit

struct PairingScanner: View {
    let scanned: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var allowed = false
    @State private var checked = false
    var body: some View {
        NavigationStack {
            Group {
                if allowed && DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    ScannerController(scanned: scanned)
                } else {
                    VStack(spacing: 16) {
                        Image(systemName: "qrcode.viewfinder").font(.largeTitle).accessibilityHidden(true)
                        Text(checked ? "Camera scanning is unavailable. Allow camera access in Settings or enter the pairing code instead."
                             : "Requesting camera access…")
                        Button("Enter code instead") { dismiss() }
                    }.padding(24)
                }
            }
            .navigationTitle("Scan host QR")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Close") { dismiss() } }
            .task {
                allowed = await AVCaptureDevice.requestAccess(for: .video)
                checked = true
            }
        }
    }
}

private struct ScannerController: UIViewControllerRepresentable {
    let scanned: (String) -> Void
    func makeUIViewController(context: Context) -> ScannerScreen { ScannerScreen(scanned: scanned) }
    func updateUIViewController(_ controller: ScannerScreen, context: Context) {}
}

private final class ScannerScreen: UIViewController, DataScannerViewControllerDelegate {
    private let scanned: (String) -> Void
    private var finished = false
    private let scanner = DataScannerViewController(
        recognizedDataTypes: [.barcode(symbologies: [.qr])], qualityLevel: .balanced,
        recognizesMultipleItems: false, isHighlightingEnabled: true)

    init(scanned: @escaping (String) -> Void) { self.scanned = scanned; super.init(nibName: nil, bundle: nil) }
    required init?(coder: NSCoder) { fatalError("Use init(scanned:)") }
    override func viewDidLoad() {
        super.viewDidLoad()
        scanner.delegate = self
        addChild(scanner)
        scanner.view.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scanner.view)
        NSLayoutConstraint.activate([
            scanner.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scanner.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scanner.view.topAnchor.constraint(equalTo: view.topAnchor),
            scanner.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        scanner.didMove(toParent: self)
    }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        do { try scanner.startScanning() }
        catch {
            let alert = UIAlertController(title: "Camera unavailable", message: "Close the scanner and enter the host pairing code instead.", preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
        }
    }
    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        scanner.stopScanning()
    }
    func dataScanner(_ scanner: DataScannerViewController, didAdd added: [RecognizedItem], allItems: [RecognizedItem]) {
        guard !finished else { return }
        for item in added {
            if case .barcode(let barcode) = item, let value = barcode.payloadStringValue {
                finished = true
                scanner.stopScanning()
                scanned(value)
                return
            }
        }
    }
}
