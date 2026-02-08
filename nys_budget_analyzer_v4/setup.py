#!/usr/bin/env python3
"""Setup script for NYS Budget Analyzer v4."""

from setuptools import setup, find_packages

setup(
    name='nys-budget-analyzer',
    version='4.0.0',
    description='God-Level NYS Budget Analysis Tool',
    author='NYS Ways and Means',
    packages=find_packages(),
    install_requires=[
        'click>=8.0',
        'pandas>=2.0',
        'pdfplumber>=0.9.0',
    ],
    extras_require={
        'excel': ['openpyxl>=3.1'],
        'html': ['jinja2>=3.0'],
        'pdf': ['PyMuPDF>=1.22'],
        'full': ['openpyxl>=3.1', 'jinja2>=3.0', 'PyMuPDF>=1.22'],
    },
    entry_points={
        'console_scripts': [
            'nys-budget=nys_budget_analyzer_v4.cli:main',
        ],
    },
    python_requires='>=3.9',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Topic :: Office/Business :: Financial',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
    ],
)
