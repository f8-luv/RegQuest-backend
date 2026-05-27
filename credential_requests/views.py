from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
from django.db.models import Count, Avg, F, Q
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth
from django.utils import timezone
from datetime import timedelta
from calendar import month_name

from .models import Request
from .serializers import RequestSerializer
from accounts.models import User
from _core.permissions import (
    IsAdminOrStaff,
    IsAdminUser,
    CanCancelOwnPendingRequest,
    IsVerifiedIfStudent
)


class RequestViewSet(viewsets.ModelViewSet):
    queryset = Request.objects.all()
    serializer_class = RequestSerializer
    permission_classes = [IsAuthenticated]

    permission_classes_by_action = {
        'list': [IsAuthenticated],
        'retrieve': [IsAuthenticated],
        'create': [IsAuthenticated, IsVerifiedIfStudent],
        'update': [IsAuthenticated, IsAdminOrStaff],
        'partial_update': [IsAuthenticated, IsAdminOrStaff],
        'destroy': [IsAuthenticated, IsAdminUser],
        'cancel': [CanCancelOwnPendingRequest],
        'dashboard': [IsAdminOrStaff],
    }

    def get_permissions(self):
        if hasattr(self, 'permission_classes_by_action') and self.action in self.permission_classes_by_action:
            return [permission() for permission in self.permission_classes_by_action[self.action]]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user

        if not user.is_authenticated:
            return Request.objects.none()

        if user.role == User.Roles.STUDENT:
            return Request.objects.filter(user=user)

        return Request.objects.all()

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['get'], url_path='track/(?P<tracking_number>[^/.]+)', permission_classes=[AllowAny])
    def track(self, request, tracking_number=None):
        req = get_object_or_404(Request, tracking_number=tracking_number)
        serializer = self.get_serializer(req)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        req = self.get_object()
        req.status = Request.Status.CANCELLED
        req.save(update_fields=['status', 'updated_at'])
        return Response(
            {"message": "Request cancelled successfully", "status": req.status},
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        now = timezone.now()

    
        daily_qs = (
            Request.objects
            .filter(created_at__gte=now - timedelta(days=6))
            .annotate(day=TruncDate('created_at'))
            .values('day', 'document_type__document_name')
            .annotate(count=Count('id'))
        )

        daily_map = {}
        for row in daily_qs:
            label = row['day'].strftime('%a')
            daily_map.setdefault(label, {})
            daily_map[label][row['document_type__document_name']] = row['count']

        WEEK_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        daily_data = []
        for i in range(6, -1, -1):
            d = now - timedelta(days=i)
            label = d.strftime('%a')
            breakdown = daily_map.get(label, {})

            daily_data.append({
                "label": label,
                "date": d.strftime('%Y-%m-%d'),
                "breakdown": breakdown,
                "total": sum(breakdown.values()) if breakdown else 0,
            })

        daily_data = sorted(daily_data, key=lambda x: WEEK_ORDER.index(x["label"]))

     
        monthly_qs = (
            Request.objects
            .filter(created_at__year=now.year)
            .annotate(month=TruncMonth('created_at'))
            .values('month', 'document_type__document_name')
            .annotate(count=Count('id'))
        )

        monthly_map = {}

        for row in monthly_qs:
            month_num = row['month'].month
            monthly_map.setdefault(month_num, {})
            monthly_map[month_num][row['document_type__document_name']] = row['count']

        monthly_data = []

        for month_num in range(1, 13):
            breakdown = monthly_map.get(month_num, {})

            monthly_data.append({
                "label": month_name[month_num],
                "breakdown": breakdown,
                "total": sum(breakdown.values()) if breakdown else 0,
            })

     

        total_requests = Request.objects.count()

        doc_type_qs = (
            Request.objects
            .values('document_type__document_name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        doc_distribution = [
            {
                'name': row['document_type__document_name'],
                'count': row['count'],
                'percentage': round(row['count'] / total_requests * 100, 1) if total_requests else 0,
            }
            for row in doc_type_qs
        ]

        staff_users = User.objects.filter(role__in=[User.Roles.STAFF, User.Roles.ADMIN])

        stats_qs = (
            Request.objects
            .filter(processed_by__in=staff_users)
            .values('processed_by')
            .annotate(
                completed=Count('id', filter=Q(status=Request.Status.COMPLETED)),
                pending=Count('id', filter=Q(status__in=[Request.Status.PENDING, Request.Status.PROCESSING])),
                avg_duration=Avg(F('processed_at') - F('created_at'), filter=Q(status=Request.Status.COMPLETED))
            )
        )

        stats_map = {row['processed_by']: row for row in stats_qs}

        staff_performance = []

        for staff in staff_users:
            stats = stats_map.get(staff.id, {})
            avg_duration = stats.get('avg_duration')

            staff_performance.append({
                'id': staff.id,
                'name': f'{staff.first_name} {staff.last_name}'.strip() or staff.username,
                'username': staff.username,
                'role': staff.role,
                'completed': stats.get('completed', 0),
                'pending': stats.get('pending', 0),
                'avg_days': round(avg_duration.total_seconds() / 86400, 1) if avg_duration else None,
            })

        staff_performance.sort(key=lambda x: x['completed'], reverse=True)

        return Response({
            'request_volume': {
                'daily': daily_data,
                'monthly': monthly_data
            },
            'document_type_distribution': doc_distribution,
            'staff_performance': staff_performance,
            'meta': {
                'total_requests': total_requests,
                'generated_at': now.isoformat()
            }
        })