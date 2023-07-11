# Always prefer setuptools over distutils
from setuptools import setup

# This call to setup() does all the work
setup(
    name="aliste",
    version="0.1.1",
    description="Aliste Smart Home SDK for Python",
    long_description="Aliste Smart Home SDK for Python",
    long_description_content_type="text/markdown",
    url="https://github.com/Kir4Kun/aliste-smart-home-sdk",
    author="Kir4Kun",
    author_email="rkbl4ze@gmail.com",
    license="MIT",
    classifiers=[
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Operating System :: OS Independent",
    ],
    packages=["aliste"],
    include_package_data=True,
)
