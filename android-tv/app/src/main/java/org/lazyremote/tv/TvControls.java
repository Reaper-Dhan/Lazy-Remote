package org.lazyremote.tv;

import android.accessibilityservice.AccessibilityService;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.AudioManager;
import android.os.Build;
import android.os.PowerManager;
import android.view.accessibility.AccessibilityEvent;

import java.util.ArrayList;
import java.util.Map;

public final class TvControls extends AccessibilityService {
    private static final Map<String, Integer> NAVIGATION = Map.of(
            "up", GLOBAL_ACTION_DPAD_UP, "down", GLOBAL_ACTION_DPAD_DOWN,
            "left", GLOBAL_ACTION_DPAD_LEFT, "right", GLOBAL_ACTION_DPAD_RIGHT,
            "enter", GLOBAL_ACTION_DPAD_CENTER, "tv_home", GLOBAL_ACTION_HOME, "tv_back", GLOBAL_ACTION_BACK);

    static volatile TvControls current;
    private boolean watchingScreen;
    private final BroadcastReceiver screen = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) TvRuntime.stop();
        }
    };

    @Override
    protected void onServiceConnected() {
        current = this;
        if (!watchingScreen) {
            registerReceiver(screen, new IntentFilter(Intent.ACTION_SCREEN_OFF), RECEIVER_NOT_EXPORTED);
            watchingScreen = true;
        }
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {}

    @Override
    public void onInterrupt() {}

    @Override
    public boolean onUnbind(Intent intent) {
        disconnect();
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        disconnect();
        super.onDestroy();
    }

    private void disconnect() {
        if (watchingScreen) {
            unregisterReceiver(screen);
            watchingScreen = false;
        }
        if (current == this) {
            current = null;
            TvRuntime.stop();
        }
    }

    boolean ready() {
        return getSystemService(PowerManager.class).isInteractive()
                && !getSystemService(KeyguardManager.class).isKeyguardLocked();
    }

    String[] keys() {
        // Android 13 can perform its built-in D-pad actions without listing them in getSystemActions.
        var keys = new ArrayList<>(NAVIGATION.keySet());
        if (Build.VERSION.SDK_INT >= 36
                && getSystemActions().stream().anyMatch(action -> action.getId() == GLOBAL_ACTION_MEDIA_PLAY_PAUSE)) {
            keys.add("media_play");
        }
        if (!getSystemService(AudioManager.class).isVolumeFixed()) {
            keys.add("vol_up");
            keys.add("vol_down");
            keys.add("vol_mute");
        }
        return keys.toArray(new String[0]);
    }

    boolean press(String name) {
        if (current != this || !ready()) return false;
        Integer navigation = NAVIGATION.get(name);
        if (navigation != null) return performGlobalAction(navigation);
        if ("media_play".equals(name)) {
            return Build.VERSION.SDK_INT >= 36 && performGlobalAction(GLOBAL_ACTION_MEDIA_PLAY_PAUSE);
        }
        int change = switch (name) {
            case "vol_up" -> AudioManager.ADJUST_RAISE;
            case "vol_down" -> AudioManager.ADJUST_LOWER;
            case "vol_mute" -> AudioManager.ADJUST_TOGGLE_MUTE;
            default -> AudioManager.ADJUST_SAME;
        };
        AudioManager audio = getSystemService(AudioManager.class);
        if (change == AudioManager.ADJUST_SAME || audio.isVolumeFixed()) return false;
        try {
            audio.adjustStreamVolume(AudioManager.STREAM_MUSIC, change, AudioManager.FLAG_SHOW_UI);
            return true;
        } catch (SecurityException e) {
            return false;
        }
    }
}
