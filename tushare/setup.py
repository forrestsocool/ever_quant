from setuptools import setup
import codecs
import os


__version__ = '1.2.11'


def read(fname):
    return codecs.open(os.path.join(os.path.dirname(__file__), fname)).read()


def read_requirements():
    requirements = []
    with open('requirements.txt', 'r') as ff:
        for line in ff.readlines():
            line = line.strip()
            if line:
                requirements.append(line)
    return requirements


long_desc = """
ShanXiZhengQuan API 
===============

山西证券 数据接口

"""


setup(
    name='sxsc_tushare',
    version=__version__,
    description='data api service',

    # 程序的详细描述
    long_description=long_desc,
    url='http://10.5.42.55:7173',
    keywords='Financial Data',

    # 程序的所属分类列表
    classifiers=[
        'Development Status :: 4 - Beta',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.2',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'License :: OSI Approved :: BSD License'
    ],

    # 需要处理的包目录（包含__init__.py的文件夹）
    packages=['sxsc_tushare'],

    # 需要安装的依赖
    install_requires=read_requirements(),
    include_package_data=True,
)
