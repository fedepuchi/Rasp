"""Capa de adquisicion: drivers de hardware + hilos productores."""

from .acquisition import AcquisitionManager, Chunk

__all__ = ["AcquisitionManager", "Chunk"]
