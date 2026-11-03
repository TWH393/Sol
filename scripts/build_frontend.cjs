// Compile ES modules without linking or executing UI code.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = path.join(root, 'companion', 'web');
const output = path.join(root, 'build', 'web');
fs.mkdirSync(output, {recursive:true});
for (const name of fs.readdirSync(source)) {
  const file=path.join(source,name);
  if (!fs.statSync(file).isFile()) continue;
  if (name.endsWith('.js')) new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{identifier:file});
  fs.copyFileSync(file,path.join(output,name));
}
process.stdout.write('Compiled browser modules without execution or UI startup.\n');
