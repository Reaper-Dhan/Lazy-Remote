package org.lazyremote.tv;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.NetworkCapabilities;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONException;
import org.json.JSONObject;

import java.net.Inet4Address;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Owns the host even when its setup activity is behind another TV app. */
public final class TvRuntime {
    private static final ExecutorService server = Executors.newSingleThreadExecutor();
    private static final ExecutorService local = Executors.newSingleThreadExecutor();
    static volatile TvRuntime current;

    volatile JSONObject pairing = new JSONObject();
    volatile String url;
    volatile String fingerprint;
    volatile String certificates;
    volatile String notice;
    volatile boolean stopped;
    private volatile boolean stopping;
    private volatile PyObject host;

    static void start(Context context) {
        if (current != null && !current.stopped) return;
        var runtime = new TvRuntime();
        current = runtime;
        Context app = context.getApplicationContext();
        server.execute(() -> runtime.run(app));
    }

    static void stop() {
        TvRuntime runtime = current;
        if (runtime == null || runtime.stopped) return;
        runtime.stopping = true;
        local.execute(() -> {
            PyObject active = runtime.host;
            if (active != null) active.callAttr("close");
        });
    }

    private void run(Context context) {
        try {
            if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
            host = Python.getInstance().getModule("android_host").callAttr("TvHost", this);
            if (!stopping) host.callAttr("run", address(context), context.getFilesDir().getAbsolutePath());
        } catch (Exception e) {
            notice = e.getMessage();
        } finally {
            stopped = true;
        }
    }

    void localAction(String action, String match) {
        local.execute(() -> {
            PyObject active = host;
            if (active != null && !stopping && !stopped) active.callAttr("local", action, match);
        });
    }

    private static String address(Context context) {
        var manager = context.getSystemService(ConnectivityManager.class);
        // Select the active physical LAN interface, never a tunnel or a wildcard address.
        var active = manager.getActiveNetwork();
        NetworkCapabilities type = manager.getNetworkCapabilities(active);
        LinkProperties properties = manager.getLinkProperties(active);
        if (type != null && properties != null
                && (type.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    || type.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))
                && !type.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
            for (LinkAddress link : properties.getLinkAddresses()) {
                if (link.getAddress() instanceof Inet4Address && link.getAddress().isSiteLocalAddress()) {
                    return link.getAddress().getHostAddress();
                }
            }
        }
        throw new IllegalStateException("Connect the TV to the same private Wi-Fi or Ethernet network as your phone.");
    }

    // Called by the shared Python host.
    public void event(String json) throws JSONException {
        var event = new JSONObject(json);
        switch (event.getString("kind")) {
            case "ready" -> {
                fingerprint = event.getString("fingerprint");
                certificates = event.getString("certificates");
                pairing = event;
                url = event.getString("url");
            }
            case "pairing" -> pairing = event;
            case "notice" -> notice = event.getString("message");
        }
    }

    public boolean ready() {
        TvControls controls = TvControls.current;
        return !stopping && controls != null && controls.ready();
    }

    public boolean press(String name) {
        TvControls controls = TvControls.current;
        return !stopping && controls != null && controls.press(name);
    }

    public String[] availableKeys() {
        TvControls controls = TvControls.current;
        return controls == null ? new String[0] : controls.keys();
    }

    public void makeIssuer(String path) throws Exception {
        CertificateFiles.issuer(path);
    }

    public void makeLeaf(String path, String address) throws Exception {
        CertificateFiles.leaf(path, address);
    }

    public boolean certificateExpires(String path, int withinDays) throws Exception {
        return CertificateFiles.expires(path, withinDays);
    }
}
