from setuptools import setup, find_packages

setup(
    name="aorc_tools",
    version="0.1.0",
    description="AORC-based wildfire risk analysis for the Western Upper Peninsula of Michigan",
    author="R2I2 Winter Resilience",
    packages=find_packages(),
    python_requires=">=3.10",
    include_package_data=True,
    package_data={"aorc_tools.explorer": ["static/*", "static/**/*", "static/vendor/*", "static/js/*", "static/geo/*"]},
    install_requires=[
        "xarray>=2023.0",
        "zarr>=2.14",
        "s3fs>=2023.0",
        "fsspec>=2023.0",
        "geopandas>=0.13",
        "shapely>=2.0",
        "pyproj>=3.5",
        "osmnx>=1.7",
        "numpy>=1.24",
        "pandas>=2.0",
        "scipy>=1.10",
        "matplotlib>=3.7",
        "Pillow>=10.0",
        "tqdm>=4.65",
        "click>=8.1",
        "astral>=3.2",
        "flask>=3.0",
        "netCDF4>=1.6",
        "tzdata>=2023.3",
        "openpyxl>=3.1",
    ],
    extras_require={
        "ingest": ["rasterio>=1.3", "pystac-client>=0.7", "planetary-computer>=1.0", "h5netcdf>=1.2", "h5py>=3.8",
                   "requests>=2.28"],
    },
    entry_points={
        "console_scripts": [
            "aorc-tools=aorc_tools.cli:main",
            "wup-explorer=aorc_tools.explorer.__main__:main",
        ],
    },
)
