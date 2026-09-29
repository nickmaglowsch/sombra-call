// Sombra spike #16: report the focused window over D-Bus (GNOME Shell 45+ ESM format).
// Throwaway prototype for question 4; the phase-2 design is in docs/adr.
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="org.sombra.Spike.WindowInfo">
    <method name="Focused">
      <arg type="s" direction="out" name="json"/>
    </method>
  </interface>
</node>`;

export default class SombraSpikeExtension extends Extension {
    enable() {
        this._impl = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._impl.export(Gio.DBus.session, '/org/sombra/Spike/WindowInfo');
    }

    disable() {
        this._impl.unexport();
        this._impl = null;
    }

    Focused() {
        const win = global.display.get_focus_window();
        if (!win)
            return JSON.stringify({title: null});
        const app = Shell.WindowTracker.get_default().get_window_app(win);
        return JSON.stringify({
            title: win.get_title(),
            wm_class: win.get_wm_class(),
            app: app ? app.get_id() : null,
            sandboxed_app_id: win.get_sandboxed_app_id(),
        });
    }
}
