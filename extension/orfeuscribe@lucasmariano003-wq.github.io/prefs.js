// SPDX-License-Identifier: GPL-3.0-or-later
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

// The app's Settings page is the only preferences window.
export default class OrfeuScribePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        try {
            Gio.Subprocess.new([`${GLib.get_home_dir()}/.local/bin/orfeuscribe`], Gio.SubprocessFlags.NONE);
        } catch (_e) {
            // Not installed: the Extensions app shows nothing more.
        }
        window.close();
    }
}
