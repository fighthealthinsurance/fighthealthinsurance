from setuptools import setup, find_packages

setup(

    name="fhi",

    version="0.1.0",

    description="Different FHI components.",

    packages=find_packages(include=['charts', 'fhi_users', 'fighthealthinsurance'])

)
