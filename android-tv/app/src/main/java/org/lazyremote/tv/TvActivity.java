package org.lazyremote.tv;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.qrcode.QRCodeWriter;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.Executors;

public final class TvActivity extends Activity {
    private static final int MUTED = Color.rgb(174, 185, 199);
    private static final int ACCENT = Color.rgb(206, 242, 155);

    private final Runnable refresh = this::refresh;
    private LinearLayout content;
    private TextView status, address, pairing, fingerprint, notice;
    private Button accessibility, start, stop, save, approve, deny, lock, forget;
    private ImageView qr;
    private String previousQr;
    private String displayedMatch;
    private String exportResult;
    private boolean exporting;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        var scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(40), dp(24), dp(40), dp(24));
        scroll.addView(content);

        TextView title = text(32, Color.WHITE);
        title.setText(R.string.app_name);
        title.setAccessibilityHeading(true);
        status = text(19, ACCENT);
        address = text(22, Color.WHITE);
        pairing = text(18, Color.WHITE);
        notice = text(16, MUTED);
        notice.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);

        var actions = row();
        accessibility = button(actions, "Open Accessibility Settings", this::openAccessibility);
        start = button(actions, "Start server", () -> { TvRuntime.start(this); refresh(); });
        stop = button(actions, "Stop server", () -> { TvRuntime.stop(); refresh(); });
        lock = button(actions, "New pairing code", () -> local("lock"));
        forget = button(actions, "Forget browser", () -> local("forget"));

        var approval = row();
        approve = button(approval, "Approve phone", () -> local("approve"));
        deny = button(approval, "Deny", () -> local("deny"));

        qr = new ImageView(this);
        qr.setContentDescription("Phone pairing QR code. You can also enter the pairing code on your phone.");
        var qrLayout = new LinearLayout.LayoutParams(dp(176), dp(176));
        qrLayout.topMargin = dp(8);
        content.addView(qr, qrLayout);

        var certificate = row();
        save = button(certificate, "Save phone certificates", this::exportCertificates);
        fingerprint = text(14, MUTED);
        setContentView(scroll);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    @Override
    protected void onPause() {
        content.removeCallbacks(refresh);
        super.onPause();
    }

    private void refresh() {
        content.removeCallbacks(refresh);
        TvRuntime runtime = TvRuntime.current;
        boolean allowed = TvControls.current != null;
        boolean running = runtime != null && !runtime.stopped;
        String url = running ? runtime.url : null;
        var state = url == null ? null : runtime.pairing;
        boolean paired = state != null && state.optBoolean("paired");
        String match = state == null || state.isNull("match") || state.optBoolean("approved")
                ? null : state.optString("match");
        String code = state == null || state.isNull("code") ? null : state.optString("code");
        String ticket = state == null || state.isNull("qr") ? null : state.optString("qr");
        show(status, !allowed ? "Enable Lazy Remote in Accessibility to send remote buttons."
                : !running ? "Server stopped" : url == null ? "Starting server…" : paired ? "Phone connected" : "Server running");
        show(address, url);
        show(pairing, match != null ? "Compare this number with your phone before approving: " + match
                : code != null ? "Phone pairing code   " + code
                : url != null && !paired ? "Open the same address on your phone to reconnect or get a new pairing code." : null);
        show(notice, exportResult != null ? exportResult : runtime == null ? null : runtime.notice);
        show(fingerprint, url == null ? null : "Certificate SHA-256   " + runtime.fingerprint);
        visible(accessibility, !allowed);
        visible(start, allowed && !running);
        visible(stop, running);
        visible(lock, url != null && match == null);
        lock.setText(paired ? "Lock phone" : "New pairing code");
        visible(forget, state != null && state.optBoolean("remembered") && match == null);
        visible(approve, match != null);
        visible(deny, match != null);
        if (match != null && !match.equals(displayedMatch)) approve.requestFocus();
        displayedMatch = match;
        visible(save, url != null);
        save.setEnabled(!exporting);
        renderQr(ticket == null || match != null ? null : url + "#pair=" + ticket);
        content.postDelayed(refresh, 750);
    }

    private void local(String action) {
        TvRuntime runtime = TvRuntime.current;
        if (runtime != null) runtime.localAction(action, displayedMatch);
    }

    private void openAccessibility() {
        try {
            startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
        } catch (ActivityNotFoundException e) {
            exportResult = "Open your TV’s Settings and find Accessibility.";
            refresh();
        }
    }

    private void renderQr(String value) {
        if (value == null) {
            visible(qr, false);
            return;
        }
        if (!value.equals(previousQr)) {
            try {
                var matrix = new QRCodeWriter().encode(value, BarcodeFormat.QR_CODE, 224, 224);
                int[] pixels = new int[matrix.getWidth() * matrix.getHeight()];
                for (int y = 0; y < matrix.getHeight(); y++) {
                    for (int x = 0; x < matrix.getWidth(); x++) {
                        pixels[y * matrix.getWidth() + x] = matrix.get(x, y) ? Color.BLACK : Color.WHITE;
                    }
                }
                qr.setImageBitmap(Bitmap.createBitmap(pixels, matrix.getWidth(), matrix.getHeight(), Bitmap.Config.ARGB_8888));
                previousQr = value;
            } catch (com.google.zxing.WriterException e) {
                visible(qr, false);
                return;
            }
        }
        visible(qr, true);
    }

    private void exportCertificates() {
        TvRuntime runtime = TvRuntime.current;
        if (runtime == null || runtime.certificates == null || exporting) return;
        Path files = Paths.get(runtime.certificates);
        exporting = true;
        var executor = Executors.newSingleThreadExecutor();
        executor.execute(() -> {
            String message;
            try {
                saveToDownloads(files.resolve("Lazy Remote Root.cer"), "application/pkix-cert");
                saveToDownloads(files.resolve("Lazy Remote.mobileconfig"), "application/x-apple-aspen-config");
                message = "Saved to Downloads → Lazy Remote TV. Transfer the phone’s file using a method you trust.";
            } catch (IOException | RuntimeException e) {
                message = "Could not save the certificates. Check free space on the TV and try again.";
            }
            String outcome = message;
            runOnUiThread(() -> { exporting = false; exportResult = outcome; });
        });
        executor.shutdown();
        refresh();
    }

    private void saveToDownloads(Path source, String type) throws IOException {
        var resolver = getContentResolver();
        var metadata = new ContentValues();
        metadata.put(MediaStore.Downloads.DISPLAY_NAME, source.getFileName().toString());
        metadata.put(MediaStore.Downloads.MIME_TYPE, type);
        metadata.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Lazy Remote TV");
        metadata.put(MediaStore.Downloads.IS_PENDING, 1);
        Uri destination = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, metadata);
        if (destination == null) throw new IOException("Downloads is unavailable.");
        try {
            try (OutputStream output = resolver.openOutputStream(destination)) {
                if (output == null) throw new IOException("Downloads is unavailable.");
                Files.copy(source, output);
            }
            var complete = new ContentValues();
            complete.put(MediaStore.Downloads.IS_PENDING, 0);
            resolver.update(destination, complete, null, null);
        } catch (IOException | RuntimeException e) {
            resolver.delete(destination, null, null);
            throw e;
        }
    }

    private TextView text(int size, int color) {
        var view = new TextView(this);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setPadding(0, dp(4), 0, dp(4));
        content.addView(view);
        return view;
    }

    private LinearLayout row() {
        var row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        content.addView(row);
        return row;
    }

    private Button button(LinearLayout row, String name, Runnable action) {
        var view = new Button(this);
        view.setText(name);
        view.setAllCaps(false);
        view.setMinHeight(dp(48));
        view.setOnClickListener(ignored -> action.run());
        row.addView(view);
        return view;
    }

    private void show(TextView view, String value) {
        if (value != null && !value.contentEquals(view.getText())) view.setText(value);
        visible(view, value != null);
    }

    private void visible(View view, boolean visible) {
        view.setVisibility(visible ? View.VISIBLE : View.GONE);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
