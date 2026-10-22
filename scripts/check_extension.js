// Parses extension modules as ES modules in GJS (syntax only; no Shell needed).
import Gio from 'gi://Gio';

for (const path of ARGV) {
    const [ok, data] = Gio.File.new_for_path(path).load_contents(null);
    if (!ok)
        throw new Error(`Could not read ${path}`);
    Reflect.parse(new TextDecoder().decode(data), {target: 'module'});
    print(`GJS ES module syntax OK: ${path}`);
}
