# -*- coding: utf-8 -*-
import django_filters
from django import forms
from .generic import (MapEntityList, MapEntityJsonList, MapEntityDetail,
                      MapEntityFormat, MapEntityLayer)
from ..filters import BaseMapEntityFilterSet
from ..models import LogEntry
from .. import registry


class LogEntryFilter(BaseMapEntityFilterSet):
    content_type = django_filters.NumberFilter(widget=forms.HiddenInput)
    object_id = django_filters.NumberFilter(widget=forms.HiddenInput)
    class Meta:
        model = LogEntry
        fields = ('user', 'content_type', 'object_id')


class LogEntryList(MapEntityList):
    model = LogEntry
    filterform = LogEntryFilter
    columns = ('id', 'action_time', 'user', 'object', 'action_flag')

    def get_queryset(self):
        queryset = super(LogEntryList, self).get_queryset()
        return queryset.filter(content_type_id__in=registry.content_type_ids)


class LogEntryJsonList(MapEntityJsonList, LogEntryList):
    pass


class LogEntryDetail(MapEntityDetail):
    model = LogEntry


class LogEntryFormat(MapEntityFormat):
    model = LogEntry
    filterform = LogEntryFilter


class LogEntryLayer(MapEntityLayer):
    model = LogEntry
