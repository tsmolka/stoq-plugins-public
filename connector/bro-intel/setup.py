from setuptools import setup, find_packages

setup(
    name="bro-intel",
    version="0.3",
    author="Marcus LaFerrera (@mlaferrera)",
    url="https://github.com/PUNCH-Cyber/stoq-plugins-public",
    license="Apache License 2.0",
    description="Save output from iocextract worker plugin formatted for Bro Intel Framework",
    packages=find_packages(),
    include_package_data=True,
)
