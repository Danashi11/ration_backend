from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Prefetch, ProtectedError, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (
    MaterialEditForm, MaterialForm, MaterialPriceForm, MixRatioForm, ProjectForm, ProjectPriceForm,
)
from .models import (
    BlockType, Material, MaterialPrice, MixRatio, Project, ProjectApproval, ProjectBlock,
)
from .permissions import (
    config_viewer_required, manager_required, price_reviewer_required, project_approver_required,
)
from .services import (
    estimate_project, latest_approved_prices, price_per_base_unit, price_per_tonne, pricing_data, save_estimate,
)
from .templatetags.ration import money


@login_required
def home_view(request):
    return redirect('project_list')


# ---------- Projects ----------

def visible_projects(user):
    projects = Project.objects.all()
    if user.sees_only_approved_projects:
        # Business rule 5: creators see reports only after approval.
        projects = projects.filter(status__in=[Project.Status.APPROVED, Project.Status.COMPLETED])
    return projects


@login_required
def project_list_view(request):
    projects = visible_projects(request.user).select_related('created_by', 'mix').prefetch_related(
        Prefetch('blocks', queryset=ProjectBlock.objects.order_by('block_type'))  # full before half
    )
    status = request.GET.get('status', '')
    query = request.GET.get('q', '').strip()
    if status in Project.Status.values:
        projects = projects.filter(status=status)
    else:
        status = ''
    if query:
        projects = projects.filter(Q(name__icontains=query) | Q(description__icontains=query))

    counts = {s: 0 for s in Project.Status.values}
    for row in visible_projects(request.user).values('status'):
        counts[row['status']] += 1
    statuses = [
        {'value': value, 'label': label, 'count': counts[value]}
        for value, label in Project.Status.choices
        if not request.user.sees_only_approved_projects or counts[value]
    ]
    pending_prices = (
        MaterialPrice.objects.filter(status=MaterialPrice.Status.PENDING).count()
        if request.user.can_review_prices else 0
    )
    pending_projects = (
        Project.objects.filter(status=Project.Status.PENDING).count()
        if request.user.can_approve_projects else 0
    )
    return render(request, 'core/project_list.html', {
        'pending_projects': pending_projects,
        'projects': projects.order_by('-created_at'),
        'statuses': statuses,
        'status': status,
        'query': query,
        'total': sum(counts.values()),
        'pending_prices': pending_prices,
    })


def _save_blocks(project, form):
    """Make the project's block lines match the quantities in the form."""
    wanted = dict(form.block_quantities())
    for block_type in BlockType.values:
        quantity = wanted.get(block_type)
        if quantity:
            ProjectBlock.objects.update_or_create(
                project=project, block_type=block_type, defaults={'quantity': quantity},
            )
        else:
            ProjectBlock.objects.filter(project=project, block_type=block_type).delete()


def _project_form_page(request, form, project=None):
    return render(request, 'core/project_form.html', {
        'form': form,
        'project': project,
        'mixes': MixRatio.objects.filter(pk__in=form.fields['mix'].queryset)
        .prefetch_related('ingredients__material').order_by('name'),
        'pricing': pricing_data(project),
    })


@manager_required
def project_new_view(request):
    form = ProjectForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            project = form.save(commit=False)
            project.created_by = request.user
            project.save()
            _save_blocks(project, form)
        messages.success(request, f'Project created with {project.mix.name}. Check the prices, then send it for approval.')
        return redirect('project_detail', pk=project.pk)
    return _project_form_page(request, form)


@manager_required
def project_edit_view(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not project.is_editable:
        return _not_editable(request, project)
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            form.save()
            _save_blocks(project, form)
        messages.success(request, 'Project saved.')
        return redirect('project_detail', pk=project.pk)
    return _project_form_page(request, form, project)


@login_required
def project_detail_view(request, pk):
    project = get_object_or_404(visible_projects(request.user).select_related('created_by', 'mix'), pk=pk)
    estimate = estimate_project(project)
    context = {
        'project': project,
        'estimate': estimate,
        'final': project.final_estimate,
        'approvals': project.approvals.select_related('reviewed_by').order_by('-reviewed_at'),
        'status_steps': _status_steps(project),
        'can_edit_prices': request.user.is_manager and project.prices_editable,
    }
    if context['final']:
        context['final_materials'] = context['final'].materials.select_related('material')
    return render(request, 'core/project_detail.html', context)


@manager_required
@require_POST
def project_price_set_view(request, pk, material_pk):
    """Set this project's price for one material. The accountant checks it when approving the project."""
    project = get_object_or_404(Project, pk=pk)
    material = get_object_or_404(Material, pk=material_pk)
    back = f"{reverse('project_detail', args=[pk])}#materials"
    if not project.prices_editable:
        return _not_editable(request, project)
    form = ProjectPriceForm(material, request.POST)
    if not form.is_valid():
        messages.error(request, f'{material.name}: {_first_error(form)}')
        return redirect(back)

    current = latest_approved_prices(project).get(material.pk)
    MaterialPrice.objects.create(
        material=material, project=project, price=form.cleaned_data['price'], unit=form.unit(),
        currency=current.currency.upper() if current else 'ZAR',
        status=MaterialPrice.Status.APPROVED, created_by=request.user,
    )
    per = 'tonne' if form.per_tonne else material.base_unit
    messages.success(request, f'{material.name} price for this project set to {form.cleaned_data["price"]} per {per}.')
    return redirect(back)


@manager_required
@require_POST
def project_price_reset_view(request, pk, material_pk):
    """Drop this project's own price for a material so the default price applies again."""
    project = get_object_or_404(Project, pk=pk)
    if not project.prices_editable:
        return _not_editable(request, project)
    material = get_object_or_404(Material, pk=material_pk)
    project.material_prices.filter(material=material).delete()
    messages.success(request, f'{material.name} uses the default price again.')
    return redirect(f"{reverse('project_detail', args=[pk])}#materials")


@manager_required
@require_POST
def project_status_view(request, pk):
    """The manager's one step from the current status: draft <-> pending, approved -> completed."""
    project = get_object_or_404(Project, pk=pk)
    status = request.POST.get('status')
    if not project.manager_next_status or status != project.manager_next_status:
        messages.error(request, 'That status change isn\'t available from here.')
        return redirect(f"{reverse('project_detail', args=[pk])}#project-status")
    back = f"{reverse('project_detail', args=[pk])}#project-status"

    if status == Project.Status.PENDING:
        if project.mix_id is None:
            messages.error(request, 'Choose a mixture before sending the project for approval.')
            return redirect(back)
        project.status = status
        project.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Project sent for approval. Only its material prices can change while it waits.')

    elif status == Project.Status.DRAFT:
        project.status = status
        project.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Project moved back to draft. Send it for approval again when it\'s ready.')

    else:  # approved -> completed
        estimate = estimate_project(project)
        if estimate['total'] is None:
            messages.error(request, 'Every material in the mixture needs a price before completing.')
            return redirect(back)
        with transaction.atomic():
            saved = save_estimate(project, request.user, estimate)
            project.status = status
            project.completed_at = timezone.now()
            project.save(update_fields=['status', 'completed_at', 'updated_at'])
        messages.success(
            request,
            f'Project completed. Its costs are saved as history ({money(saved.total_material_cost, saved.currency)}).',
        )
    return redirect('project_detail', pk=pk)


@project_approver_required
@require_POST
def project_review_view(request, pk):
    """Accountant decision on a pending project: approve it, or decline it back to draft."""
    project = get_object_or_404(Project, pk=pk)
    back = f"{reverse('project_detail', args=[pk])}#project-status"
    if project.status != Project.Status.PENDING:
        messages.error(request, 'Only projects waiting for approval can be reviewed.')
        return redirect(back)

    decision = request.POST.get('decision')
    comments = request.POST.get('comments', '').strip()
    if decision not in ProjectApproval.Decision.values:
        messages.error(request, 'Choose approve or decline.')
        return redirect(back)
    if decision == ProjectApproval.Decision.DECLINED and not comments:
        messages.error(request, 'Say what needs changing so the manager can fix it.')
        return redirect(back)
    if decision == ProjectApproval.Decision.APPROVED and estimate_project(project)['total'] is None:
        messages.error(request, 'This project can\'t be approved until every material in its mixture has a price.')
        return redirect(back)

    with transaction.atomic():
        ProjectApproval.objects.create(
            project=project, reviewed_by=request.user, decision=decision, comments=comments or None,
        )
        project.status = Project.Status.APPROVED if decision == ProjectApproval.Decision.APPROVED else Project.Status.DRAFT
        project.save(update_fields=['status', 'updated_at'])

    if decision == ProjectApproval.Decision.APPROVED:
        messages.success(request, f'"{project.name}" approved.')
    else:
        messages.success(request, f'"{project.name}" declined and sent back to the manager as a draft.')
    return redirect('project_detail', pk=pk)


@manager_required
@require_POST
def project_delete_view(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not project.is_editable:
        return _not_editable(request, project)
    try:
        project.delete()
    except ProtectedError:
        messages.error(request, f'"{project.name}" has saved estimates or approvals, so it can\'t be deleted.')
        return redirect('project_detail', pk=pk)
    messages.success(request, f'Project "{project.name}" deleted.')
    return redirect('project_list')


# ---------- Mixes, materials and prices ----------

@config_viewer_required
def config_view(request):
    prices = latest_approved_prices()
    materials = list(Material.objects.order_by('-is_active', 'name'))
    for material in materials:
        material.current_price = prices.get(material.pk)
        material.current_unit_price = (
            price_per_base_unit(material.current_price) if material.current_price else None
        )
        material.current_tonne_price = price_per_tonne(material, material.current_unit_price)
    context = {
        'mixes': MixRatio.objects.prefetch_related('ingredients__material').order_by('-is_active', 'name'),
        'materials': materials,
        'pending_count': MaterialPrice.objects.filter(status=MaterialPrice.Status.PENDING).count(),
        'prices': MaterialPrice.objects.select_related('material', 'project', 'created_by', 'reviewed_by')
        .order_by('-created_at')[:40],
        'material_form': MaterialForm(prefix='material'),
        'price_form': MaterialPriceForm(prefix='price'),
    }
    return render(request, 'core/config.html', context)


@manager_required
@require_POST
def material_add_view(request):
    form = MaterialForm(request.POST, prefix='material')
    if form.is_valid():
        form.save()
        messages.success(request, f'Material "{form.instance.name}" added.')
    else:
        messages.error(request, _first_error(form))
    return redirect('config')


@manager_required
def material_edit_view(request, pk):
    material = get_object_or_404(Material, pk=pk)
    form = MaterialEditForm(request.POST or None, instance=material)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Material "{material.name}" saved.')
        return redirect('config')
    used = material.mix_ingredients.exists() or material.prices.exists()
    return render(request, 'core/material_form.html', {'form': form, 'material': material, 'used': used})


@manager_required
@require_POST
def material_delete_view(request, pk):
    material = get_object_or_404(Material, pk=pk)
    try:
        material.delete()
    except ProtectedError:
        messages.error(
            request,
            f'"{material.name}" is used in mixes or prices, so it can\'t be deleted. '
            'Mark it unavailable instead.',
        )
        return redirect('material_edit', pk=pk)
    messages.success(request, f'Material "{material.name}" deleted.')
    return redirect('config')


@manager_required
@require_POST
def price_add_view(request):
    form = MaterialPriceForm(request.POST, prefix='price')
    if form.is_valid():
        price = form.save(commit=False)
        price.created_by = request.user
        price.save()
        messages.success(request, f'Price for {price.material} sent for approval.')
    else:
        messages.error(request, _first_error(form))
    return redirect('config')


@manager_required
@require_POST
def price_delete_view(request, pk):
    # Reviewed prices stay as history (business rule 2); only pending ones can be withdrawn.
    price = get_object_or_404(MaterialPrice.objects.select_related('project'), pk=pk,
                              status=MaterialPrice.Status.PENDING)
    if price.project and not price.project.prices_editable:
        return _not_editable(request, price.project)
    price.delete()
    messages.success(request, f'Pending price for {price.material} withdrawn.')
    return _back(request)


@price_reviewer_required
@require_POST
def price_review_view(request, pk):
    price = get_object_or_404(MaterialPrice.objects.select_related('material'), pk=pk,
                              status=MaterialPrice.Status.PENDING)
    decision = request.POST.get('decision')
    if decision not in (MaterialPrice.Status.APPROVED, MaterialPrice.Status.DECLINED):
        messages.error(request, 'Choose approve or decline.')
        return _back(request)

    corrected = None
    raw = request.POST.get('corrected_price', '').strip().replace(',', '.')
    if decision == MaterialPrice.Status.DECLINED and raw:
        try:
            corrected = Decimal(raw)
        except InvalidOperation:
            corrected = None
        if corrected is None or corrected <= 0 or corrected.as_tuple().exponent < -2:
            messages.error(request, 'Enter the correct price as a positive amount, e.g. 16.43.')
            return _back(request)

    now = timezone.now()
    with transaction.atomic():
        price.status = decision
        price.reviewed_by = request.user
        price.reviewed_at = now
        price.save()
        if corrected is not None:
            # The accountant's own figure replaces the declined one and is approved at once.
            MaterialPrice.objects.create(
                material=price.material, project=price.project, price=corrected, unit=price.unit,
                currency=price.currency,
                status=MaterialPrice.Status.APPROVED, created_by=request.user,
                reviewed_by=request.user, reviewed_at=now,
            )

    if corrected is not None:
        messages.success(request, f'Price for {price.material} declined and set to {corrected} {price.currency}.')
    else:
        messages.success(request, f'Price for {price.material} {decision}.')
    return _back(request)


@manager_required
def mix_edit_view(request, pk=None):
    mix = get_object_or_404(MixRatio, pk=pk) if pk else MixRatio()
    form = MixRatioForm(request.POST or None, instance=mix)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            mix = form.save()
        messages.success(request, f'Mix "{mix.name}" saved.')
        return redirect('config')
    return render(request, 'core/mix_form.html', {'form': form, 'mix': mix})


@manager_required
@require_POST
def mix_delete_view(request, pk):
    mix = get_object_or_404(MixRatio, pk=pk)
    try:
        mix.delete()
    except ProtectedError:
        messages.error(request, f'"{mix.name}" is used by projects, so it can\'t be deleted. Untick Active instead.')
        return redirect('mix_edit', pk=pk)
    messages.success(request, f'Mix "{mix.name}" deleted.')
    return redirect('config')


def _status_steps(project):
    """Draft, pending, approved, completed, each marked done, current or upcoming."""
    values = Project.Status.values
    current = values.index(project.status)
    return [
        {'label': label, 'state': 'done' if i < current else 'current' if i == current else 'upcoming'}
        for i, (value, label) in enumerate(Project.Status.choices)
    ]


def _not_editable(request, project):
    if project.is_locked:
        reason = 'is completed and kept as history, so it can\'t be changed'
    elif project.status == Project.Status.PENDING:
        reason = 'is waiting for approval. Only its material prices can change; move it back to draft to edit the rest'
    else:
        reason = 'is approved, so it can\'t be changed'
    messages.error(request, f'"{project.name}" {reason}.')
    return redirect('project_detail', pk=project.pk)


def _back(request):
    """Redirect to the page the form came from (the 'next' field), or the config page."""
    target = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect('config')


def _first_error(form):
    for errors in form.errors.values():
        return errors[0]
    return 'Please check the form.'
