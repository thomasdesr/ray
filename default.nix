{ sources ? import ./nix/sources.nix, pkgs ? import sources.nixpkgs { } }:
with pkgs;

let
  python = python37;
  pythonPackages = python.pkgs;

  rayDependencies = {
    # What do you need to get everything to "build"
    build_core = [
      bazel

      python
      unzip
      cacert
      git
      which
    ];

    streaming = [
      jdk
    ];
  };
  flattenedRayDependencies = lib.attrsets.attrValues rayDependencies;

  # rayPkg = pythonPackages.buildPythonPackage {
  #      pname = "ray";
  #      version = "head";

  #      nativeBuildInputs = [ bazel cacert git which unzip ];

  #      src = ./python;

  #      # Avoid compiling the embedded third party libraries
  #      SKIP_THIRDPARTY_INSTALL=true;
  #    };

in stdenv.mkDerivation rec {
  name = "impurePythonEnv";

  buildInputs = [
      # Tests
      # procps
      # pythonPackages.numba # lots of custom patches here to make llvm happy
      # libstdcxx5 # many things use this
      # gdb
      
      glib # To get opencv working


      # gtk2
      # zlib
      # mesa

      # rayPkg
  ] ++ flattenedRayDependencies;

  shellHook = ''
    set -euo pipefail

    SOURCE_DATE_EPOCH=$(date +%s)

    [[ ! -d venv ]] && python -m venv venv
    #venv/bin/pip install -e ./python
    source venv/bin/activate

    # Turn these back off for the user's shell
    set +euo pipefail
  '';
}

