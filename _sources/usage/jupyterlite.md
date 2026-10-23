# JupyterLite Xeus


## Installation

To consume emscripten-forge packages in a JupyterLite environment, the `jupyterlite_xeus` package needs
to be installed. This can be done with `pip`

```bash
pip install jupyterlite_xeus
```
or `conda`/`mamba`/`micromamba`

```
mamba install jupyterlite-xeus
```

## Usage

```{note}
Emscripten-forge provides xeus kernels for multiple languages, this document focuses on the Python kernel, namely `xeus-python`.
While the other kernels can also be installed as described below, adding custom packages is only supported for the `xeus-python` kernel at the moment.
```

### From environment file

To load a xeus-python kernel with a custom environment, create an environment.yaml file with xeus-python and the desired dependencies. Here is an example with numpy as a additional dependency:
```yaml
name: xeus-lite-wasm
channels:
  - oci://ghcr.io/wangyenshu/emscripten-forge-4x
  - conda-forge
dependencies:
  - xeus-python
  - numpy
```

`jupyterlite_xeus` installs these packages with micromamba, which needs to be version 2.0 or later
to read the `oci://` channel.

To build JupyterLite, run the following command where environment.yaml is the path to the file you just created

```bash
jupyter lite build --XeusAddon.environment_file=some_path/to/environment.yaml
```

```{note}
The packages are downloaded when the site is built and are served with it.
Installing more emscripten-forge packages from inside a running notebook doesn't work with
this channel, because browsers can't download from ghcr.io; add them to the environment file instead.
```

### From prefix
Create a environment with the desired packages. Here is an example with numpy as a additional dependency

```bash
micromamba create \
    -n myenv \
    --platform=emscripten-wasm32 \
    -c oci://ghcr.io/wangyenshu/emscripten-forge-4x \
    -c conda-forge \
    --yes \
    "python>=3.11"  numpy pandas xeus-python
```

Use the following command to build JupyterLite.

```sh
jupyter lite build --XeusAddon.prefix=$MAMBA_ROOT_PREFIX/envs/myenv
```
