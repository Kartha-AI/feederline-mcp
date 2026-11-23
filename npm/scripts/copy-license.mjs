// The npm tarball carries the repository's LICENSE.
import { copyFileSync } from "node:fs";
copyFileSync(new URL("../../LICENSE", import.meta.url), new URL("../LICENSE", import.meta.url));
