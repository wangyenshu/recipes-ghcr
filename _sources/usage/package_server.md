# Package Server

Emscripten-forge packages are hosted on the [GitHub Container Registry](https://github.com/wangyenshu?tab=packages&repo_name=recipes-ghcr)
as an OCI conda channel: `oci://ghcr.io/wangyenshu/emscripten-forge-4x`.

````{note}
To use emscripten-forge conda packages, you need to add the `emscripten-forge` channel to your conda configuration or use the `--channel` flag when installing packages, ie:

```bash
micromamba create -n myenv --platform=emscripten-wasm32 \
    -c oci://ghcr.io/wangyenshu/emscripten-forge-4x \
    -c conda-forge \
    python numpy
```

`oci://` channels need micromamba 2.0 or later, or pixi.
````
